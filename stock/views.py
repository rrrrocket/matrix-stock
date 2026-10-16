import json
import hashlib
import hmac
import os
import secrets
import uuid
from collections import defaultdict
from datetime import timedelta
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone

from .models import ErpLinkGrant, ExternalOrder, OrderLine, OutboundDocument, StockBalance, StockExpiryAlert, StockItem, StockMovement, StockNotificationSettings, Warehouse
from .forms import StockRegistrationForm
from .services import StockError, adjust_stock, parse_cost, parse_expiry_date, record_batch_inbound, record_inbound, record_manual_outbound, record_outbound, record_batch_order_outbound, upsert_external_order
from .erp_callback import send_outbound_callback
from .notifications import parse_expiry_reminder_days


def staff_required(view):
    @wraps(view)
    @login_required
    def inner(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied("只有库存系统员工可以访问")
        return view(request, *args, **kwargs)

    return inner


def register(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("stock_admin_users" if request.user.is_superuser else "dashboard")
    form = StockRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "注册申请已提交。请联系管理员审核账号，通过后即可登录。")
        return redirect("login")
    return render(request, "stock/register.html", {"form": form})


def _inventory_rows(owner, query="", warehouse_id=""):
    balances = StockBalance.objects.select_related("warehouse", "item").filter(warehouse__owner=owner, on_hand__gt=0)
    if query:
        balances = balances.filter(Q(item__sku__icontains=query) | Q(item__name__icontains=query) | Q(note__icontains=query))
    if warehouse_id:
        balances = balances.filter(warehouse_id=warehouse_id)
    return balances.order_by("item__sku", "warehouse__name")[:500]


def _order_rows(owner, status_filter="open", query=""):
    lines = OrderLine.objects.select_related("order").filter(order__owner=owner, is_present=True)
    if status_filter == "open":
        lines = lines.filter(order__status=ExternalOrder.Status.OPEN, is_skipped=False, quantity__gt=0)
    elif status_filter == "paused":
        lines = lines.filter(order__status=ExternalOrder.Status.OPEN, is_skipped=True)
    elif status_filter == "closed":
        lines = lines.filter(order__status__in=[ExternalOrder.Status.CANCELLED, ExternalOrder.Status.CLOSED])
    if query:
        lines = lines.filter(Q(sku__icontains=query) | Q(order__external_order_id__icontains=query) | Q(order__store_name__icontains=query))
    lines = list(lines.order_by("-order__received_at", "-id")[:250])
    skus = {line.sku for line in lines}
    stock_by_sku = defaultdict(list)
    for balance in StockBalance.objects.select_related("warehouse", "item").filter(warehouse__owner=owner, item__sku__in=skus, on_hand__gt=0, warehouse__is_active=True):
        stock_by_sku[balance.item.sku].append(balance)
    for line in lines:
        line.stock_options = stock_by_sku[line.sku]
        line.total_available = sum(balance.on_hand for balance in line.stock_options)
        line.needs_action = (
            line.order.status == ExternalOrder.Status.OPEN and line.is_present
            and not line.is_skipped and line.remaining_quantity > 0
            and line.total_available > 0
        )
        line.default_outbound_quantity = min(line.remaining_quantity, line.stock_options[0].on_hand) if line.stock_options else 0
        line.operation_id = uuid.uuid4()
    return lines


@staff_required
@require_GET
def dashboard(request):
    rows = [line for line in _order_rows(request.user) if line.needs_action]
    context = {
        "active_nav": "dashboard",
        "warehouse_count": Warehouse.objects.filter(owner=request.user, is_active=True).count(),
        "sku_count": StockBalance.objects.filter(warehouse__owner=request.user, on_hand__gt=0).values("item_id").distinct().count(),
        "stock_total": StockBalance.objects.filter(warehouse__owner=request.user).aggregate(total=Sum("on_hand"))["total"] or 0,
        "pending_count": len(rows),
        "pending_rows": rows[:8],
        "recent_movements": StockMovement.objects.select_related("warehouse", "item", "operator").filter(warehouse__owner=request.user)[:6],
    }
    return render(request, "stock/dashboard.html", context)


@staff_required
def warehouses(request):
    if request.method == "POST":
        code = str(request.POST.get("code") or "").strip()
        name = str(request.POST.get("name") or "").strip()
        if not code or not name or len(code) > 40 or len(name) > 100:
            messages.error(request, "请填写有效的仓库编码和名称")
        elif Warehouse.objects.filter(owner=request.user, code=code).exists():
            messages.error(request, "仓库编码已存在")
        else:
            Warehouse.objects.create(owner=request.user, code=code, name=name)
            messages.success(request, "仓库已创建")
        return redirect("warehouses")
    return render(request, "stock/warehouses.html", {
        "active_nav": "warehouses", "warehouses": Warehouse.objects.filter(owner=request.user, is_active=True),
        "notification_settings": StockNotificationSettings.objects.filter(owner=request.user).first(),
        "expiry_alert_errors": StockExpiryAlert.objects.select_related("balance__warehouse", "balance__item").filter(
            balance__warehouse__owner=request.user, status=StockExpiryAlert.Status.PENDING, attempts__gt=0,
        ).order_by("-id")[:10],
    })


def _valid_robot_url(value, hosts):
    if not value:
        return True
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname in hosts and not parsed.username
                and not parsed.password and parsed.port is None and len(value) <= 500)
    except ValueError:
        return False


@staff_required
@require_POST
def update_notification_settings(request):
    feishu = request.POST.get("feishu_webhook", "").strip()
    wecom = request.POST.get("wecom_webhook", "").strip()
    try:
        days = parse_expiry_reminder_days(request.POST.get("expiry_reminder_days", "7,3,1"))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("warehouses")
    if not _valid_robot_url(feishu, {"open.feishu.cn", "open.larksuite.com"}) or not _valid_robot_url(wecom, {"qyapi.weixin.qq.com"}):
        messages.error(request, "请填写有效的机器人地址")
        return redirect("warehouses")
    settings_row, _ = StockNotificationSettings.objects.get_or_create(owner=request.user)
    settings_row.expiry_reminder_days = ",".join(map(str, days))
    settings_row.feishu_webhook = feishu
    settings_row.wecom_webhook = wecom
    if request.POST.get("clear_feishu_secret") == "1":
        settings_row.feishu_secret = ""
    elif request.POST.get("feishu_secret", "").strip():
        settings_row.feishu_secret = request.POST["feishu_secret"].strip()[:200]
    settings_row.save()
    messages.success(request, "到期提醒设置已保存")
    return redirect("warehouses")


@staff_required
@require_POST
@transaction.atomic
def delete_warehouse(request, warehouse_id):
    warehouse = get_object_or_404(Warehouse.objects.select_for_update(), pk=warehouse_id, owner=request.user, is_active=True)
    balances = StockBalance.objects.filter(warehouse=warehouse)
    if balances.filter(on_hand__gt=0).exists():
        messages.error(request, "仓库仍有库存，请先清空库存")
    elif StockMovement.objects.filter(warehouse=warehouse).exists():
        warehouse.is_active = False
        warehouse.save(update_fields=["is_active"])
        messages.success(request, "仓库已删除，历史流水已保留")
    else:
        balances.delete()
        warehouse.delete()
        messages.success(request, "仓库已删除")
    return redirect("warehouses")


@staff_required
@require_GET
def inventory(request):
    query = request.GET.get("q", "").strip()
    warehouse_id = request.GET.get("warehouse", "").strip()
    return render(request, "stock/inventory.html", {
        "active_nav": "inventory",
        "balances": _inventory_rows(request.user, query, warehouse_id),
        "warehouses": Warehouse.objects.filter(owner=request.user, is_active=True),
        "query": query,
        "selected_warehouse": warehouse_id,
    })


@staff_required
@require_POST
def inbound(request):
    try:
        movement = record_inbound(
            warehouse_id=request.POST.get("warehouse_id"),
            sku=request.POST.get("sku"),
            quantity=request.POST.get("quantity"),
            product_name=request.POST.get("product_name"),
            note=request.POST.get("note"),
            expires_on=request.POST.get("expires_on"),
            operator=request.user,
        )
        messages.success(request, f"{movement.item.sku} 已入库 {movement.quantity_change} 件")
    except (StockError, Warehouse.DoesNotExist) as exc:
        messages.error(request, str(exc) if isinstance(exc, StockError) else "仓库不存在")
    return redirect("inventory")


@staff_required
@require_POST
def batch_inbound(request):
    skus = request.POST.getlist("sku")
    quantities = request.POST.getlist("quantity")
    notes = request.POST.getlist("note")
    costs = request.POST.getlist("unit_cost")
    expiries = request.POST.getlist("expires_on")
    if not costs:
        costs = [""] * len(skus)
    if not expiries:
        expiries = [""] * len(skus)
    if len(skus) != len(quantities) or len(skus) != len(notes) or len(skus) != len(costs) or len(skus) != len(expiries):
        messages.error(request, "入库数据不完整")
        return redirect("inventory")
    rows = [
        {"sku": sku, "quantity": quantity, "note": note, "unit_cost": cost, "expires_on": expiry}
        for sku, quantity, note, cost, expiry in zip(skus, quantities, notes, costs, expiries)
    ]
    try:
        movements = record_batch_inbound(
            warehouse_id=request.POST.get("warehouse_id"),
            rows=rows,
            default_quantity=request.POST.get("default_quantity"),
            operator=request.user,
        )
        messages.success(request, f"已入库 {len(movements)} 条，共 {sum(row.quantity_change for row in movements)} 件")
    except StockError as exc:
        messages.error(request, str(exc))
    return redirect("inventory")


@staff_required
@require_POST
def edit_inventory_quantity(request, balance_id):
    balance = get_object_or_404(
        StockBalance.objects.select_related("warehouse", "item"),
        pk=balance_id, warehouse__owner=request.user, warehouse__is_active=True,
    )
    is_async = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        movement = adjust_stock(
            warehouse_id=balance.warehouse_id,
            sku=balance.item.sku,
            new_quantity=request.POST.get("new_quantity"),
            expected_quantity=request.POST.get("expected_quantity"),
            note="库存明细直接编辑",
            movement_type=StockMovement.Type.EDIT,
            operator=request.user,
        )
        message = f"{movement.item.sku} 已更新为 {movement.after_quantity} 件"
        if is_async:
            balance.refresh_from_db()
            return JsonResponse({"message": message, "quantity": movement.after_quantity,
                                 "updated_at": timezone.localtime(balance.updated_at).strftime("%m-%d %H:%M")})
        messages.success(request, message)
    except StockError as exc:
        if is_async:
            balance.refresh_from_db()
            return JsonResponse({"error": str(exc), "current_quantity": balance.on_hand}, status=409)
        messages.error(request, str(exc))
    return redirect("inventory")


@staff_required
@require_POST
@transaction.atomic
def update_inventory_cost(request, balance_id):
    balance = get_object_or_404(StockBalance.objects.select_for_update().select_related("item", "warehouse"),
                                pk=balance_id, warehouse__owner=request.user, warehouse__is_active=True)
    is_async = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        cost = parse_cost(request.POST.get("unit_cost"), required=True)
        balance.avg_unit_cost = cost
        balance.save(update_fields=["avg_unit_cost", "updated_at"])
        balance.item.reference_cost = cost
        balance.item.save(update_fields=["reference_cost"])
        message = f"{balance.item.sku} 成本单价已保存"
        if is_async:
            return JsonResponse({"message": message, "unit_cost": format(cost.normalize(), "f"),
                                 "updated_at": timezone.localtime(balance.updated_at).strftime("%m-%d %H:%M")})
        messages.success(request, message)
    except StockError as exc:
        if is_async:
            return JsonResponse({"error": str(exc)}, status=400)
        messages.error(request, str(exc))
    return redirect("inventory")


@staff_required
@require_POST
@transaction.atomic
def update_inventory_note(request, balance_id):
    balance = get_object_or_404(StockBalance.objects.select_for_update().select_related("warehouse", "item"),
                                pk=balance_id, warehouse__owner=request.user, warehouse__is_active=True)
    note = request.POST.get("note", "").strip()
    is_async = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    expected_note = request.POST.get("expected_note")
    if expected_note is not None and balance.note != expected_note:
        if is_async:
            return JsonResponse({"error": "库存备注已被修改，请核对后重试", "current_value": balance.note}, status=409)
        messages.error(request, "库存备注已被修改，请刷新后重试")
        return redirect("inventory")
    if len(note) > 500:
        if is_async:
            return JsonResponse({"error": "备注不能超过 500 字符"}, status=400)
        messages.error(request, "备注不能超过 500 字符")
        return redirect("inventory")
    balance.note = note
    balance.save(update_fields=["note", "updated_at"])
    message = f"{balance.item.sku} 库存备注已保存"
    if is_async:
        return JsonResponse({"message": message, "note": note,
                             "updated_at": timezone.localtime(balance.updated_at).strftime("%m-%d %H:%M")})
    messages.success(request, message)
    return redirect("inventory")


@staff_required
@require_POST
@transaction.atomic
def update_inventory_expiry(request, balance_id):
    balance = get_object_or_404(StockBalance.objects.select_for_update().select_related("warehouse", "item"),
                                pk=balance_id, warehouse__owner=request.user, warehouse__is_active=True)
    is_async = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    expected_expiry = request.POST.get("expected_expiry")
    current_expiry = balance.expires_on.isoformat() if balance.expires_on else ""
    if expected_expiry is not None and current_expiry != expected_expiry:
        if is_async:
            return JsonResponse({"error": "过期时间已被修改，请核对后重试", "current_value": current_expiry}, status=409)
        messages.error(request, "过期时间已被修改，请刷新后重试")
        return redirect("inventory")
    try:
        expiry = parse_expiry_date(request.POST.get("expires_on"))
    except StockError as exc:
        if is_async:
            return JsonResponse({"error": str(exc)}, status=400)
        messages.error(request, str(exc))
        return redirect("inventory")
    if balance.expires_on != expiry:
        balance.expires_on = expiry
        balance.save(update_fields=["expires_on", "updated_at"])
        StockExpiryAlert.objects.filter(balance=balance).delete()
    message = f"{balance.item.sku} 过期时间已保存"
    if is_async:
        return JsonResponse({"message": message, "expires_on": expiry.isoformat() if expiry else "",
                             "updated_at": timezone.localtime(balance.updated_at).strftime("%m-%d %H:%M")})
    messages.success(request, message)
    return redirect("inventory")


@staff_required
@require_GET
def orders(request):
    status_filter = request.GET.get("status", "open")
    if status_filter not in {"open", "paused", "closed", "all"}:
        status_filter = "open"
    query = request.GET.get("q", "").strip()
    rows = _order_rows(request.user, status_filter, query)
    if status_filter == "open":
        rows = [line for line in rows if line.remaining_quantity > 0]
    return render(request, "stock/orders.html", {
        "active_nav": "orders", "lines": rows, "status_filter": status_filter, "query": query,
    })


@staff_required
@require_POST
def outbound(request, line_id):
    try:
        movement = record_outbound(
            line_id=line_id,
            warehouse_id=request.POST.get("warehouse_id"),
            quantity=request.POST.get("quantity"),
            tracking_number=request.POST.get("tracking_number"),
            operation_id=request.POST.get("operation_id"),
            operator=request.user,
        )
        synced = send_outbound_callback(movement.id)
        if synced:
            messages.success(request, f"已从 {movement.warehouse.name} 出库 {-movement.quantity_change} 件；国内快递单号已回填 ERP")
        else:
            messages.warning(request, f"已从 {movement.warehouse.name} 出库 {-movement.quantity_change} 件；ERP 回填失败，系统将自动重试，请在出入库流水查看原因")
    except (StockError, Warehouse.DoesNotExist, OrderLine.DoesNotExist) as exc:
        messages.error(request, str(exc) if isinstance(exc, StockError) else "订单商品或仓库不存在")
    return redirect("orders")


@staff_required
@require_POST
def batch_order_outbound(request):
    try:
        rows = json.loads(request.POST.get("rows", ""))
        movements = record_batch_order_outbound(rows=rows, operator=request.user)
        synced_count = sum(send_outbound_callback(movement.id) for movement in movements)
        summary = f"已确认出库 {len(movements)} 条，共 {sum(-movement.quantity_change for movement in movements)} 件"
        if synced_count == len(movements):
            messages.success(request, f"{summary}；国内快递单号已回填 ERP")
        else:
            messages.warning(request, f"{summary}；{len(movements) - synced_count} 条 ERP 回填失败，系统将自动重试，请在出入库流水查看原因")
        return redirect("orders")
    except (ValueError, TypeError, StockError, Warehouse.DoesNotExist, OrderLine.DoesNotExist) as exc:
        messages.error(request, str(exc) if isinstance(exc, StockError) else "批量出库数据无效")
        return redirect("orders")


@staff_required
def manual_outbound(request):
    if request.method == "POST":
        skus = request.POST.getlist("sku")
        quantities = request.POST.getlist("quantity")
        try:
            if len(skus) != len(quantities):
                raise StockError("出库数据不完整")
            document = record_manual_outbound(
                warehouse_id=request.POST.get("warehouse_id"),
                rows=[{"sku": sku, "quantity": quantity} for sku, quantity in zip(skus, quantities)],
                purpose=request.POST.get("purpose"), recipient=request.POST.get("recipient"),
                note=request.POST.get("note"), operator=request.user, request_id=request.POST.get("request_id"),
            )
            messages.success(request, f"出库单 {document.document_no} 已生成")
            return redirect("outbound_document", document_id=document.id)
        except (StockError, Warehouse.DoesNotExist) as exc:
            messages.error(request, str(exc) if isinstance(exc, StockError) else "仓库不存在")
    return render(request, "stock/manual_outbound.html", {
        "active_nav": "manual_outbound", "warehouses": Warehouse.objects.filter(owner=request.user, is_active=True),
        "request_id": uuid.uuid4(),
    })


@staff_required
@require_GET
def outbound_stock_search(request):
    warehouse_id = request.GET.get("warehouse_id")
    query = request.GET.get("q", "").strip()
    if not warehouse_id or not warehouse_id.isdecimal() or not query:
        return JsonResponse({"items": []})
    rows = StockBalance.objects.select_related("item").filter(
        warehouse_id=warehouse_id, warehouse__owner=request.user, warehouse__is_active=True,
        item__sku__icontains=query, on_hand__gt=0,
    ).order_by("item__sku")[:20]
    return JsonResponse({"items": [{"sku": row.item.sku, "available": row.on_hand,
                                    "unit_cost": str(row.avg_unit_cost) if row.avg_unit_cost is not None else None}
                                   for row in rows]})


@staff_required
@require_GET
def outbound_documents(request):
    return render(request, "stock/outbound_documents.html", {
        "active_nav": "manual_outbound",
        "documents": OutboundDocument.objects.select_related("warehouse", "operator").filter(
            owner=request.user, source=OutboundDocument.Source.MANUAL)[:200],
    })


@staff_required
@require_GET
def outbound_document(request, document_id):
    document = get_object_or_404(OutboundDocument.objects.select_related("warehouse", "operator")
                                 .prefetch_related("lines"), pk=document_id, owner=request.user)
    return render(request, "stock/outbound_document.html", {"active_nav": "manual_outbound", "document": document})


@staff_required
@require_POST
def skip_order_line(request, line_id):
    line = get_object_or_404(OrderLine.objects.select_related("order"), pk=line_id, order__owner=request.user)
    if line.order.status != ExternalOrder.Status.OPEN:
        messages.error(request, "订单已结束")
    else:
        line.is_skipped = True
        line.save(update_fields=["is_skipped"])
        messages.success(request, "已暂不处理")
        return redirect(f"{reverse('orders')}?status=paused")
    return redirect("orders")


@staff_required
@require_POST
def resume_order_line(request, line_id):
    line = get_object_or_404(OrderLine.objects.select_related("order"), pk=line_id, order__owner=request.user)
    if line.order.status != ExternalOrder.Status.OPEN or not line.is_present:
        messages.error(request, "订单已结束")
    else:
        line.is_skipped = False
        line.save(update_fields=["is_skipped"])
        messages.success(request, "已继续处理")
    return redirect("orders")


@staff_required
@require_GET
def movements(request):
    query = request.GET.get("q", "").strip()
    callback_filter = request.GET.get("callback", "").strip()
    if callback_filter not in {"", "failed", "pending", "sent"}:
        callback_filter = ""
    rows = StockMovement.objects.select_related("warehouse", "item", "operator", "order_line__order", "document_line__document").filter(warehouse__owner=request.user)
    if callback_filter:
        rows = rows.filter(movement_type=StockMovement.Type.OUTBOUND)
        if callback_filter == "failed":
            rows = rows.filter(erp_callback_sent_at__isnull=True, erp_callback_attempts__gt=0)
        elif callback_filter == "pending":
            rows = rows.filter(erp_callback_sent_at__isnull=True, erp_callback_attempts=0)
        else:
            rows = rows.filter(erp_callback_sent_at__isnull=False)
    if query:
        rows = rows.filter(Q(item__sku__icontains=query) | Q(order_line__order__external_order_id__icontains=query))
    return render(request, "stock/movements.html", {
        "active_nav": "movements", "movements": rows[:300], "query": query, "callback_filter": callback_filter,
    })


@staff_required
@require_POST
def retry_outbound_callback(request, movement_id):
    movement = get_object_or_404(
        StockMovement, pk=movement_id, warehouse__owner=request.user,
        movement_type=StockMovement.Type.OUTBOUND,
    )
    if send_outbound_callback(movement.id, force=True):
        messages.success(request, f"{movement.tracking_number} 已由 ERP 确认")
    else:
        messages.warning(request, f"{movement.tracking_number} 回填仍失败，系统将继续自动重试；请在流水查看原因")
    return redirect("movements")


@require_GET
def health(request):
    return JsonResponse({"status": "ok"})


@staff_required
@require_GET
def erp_connect(request):
    """Complete the browser login leg of an ERP↔Stock account link."""
    state = request.GET.get("state", "")
    return_url = request.GET.get("return_url", "")
    signature = request.GET.get("signature", "")
    key = os.environ.get("STOCK_ERP_API_KEY", "")
    if not key or not state or not return_url or len(state) > 1000 or len(return_url) > 500:
        return JsonResponse({"error": "绑定请求无效"}, status=400)
    parsed = urlsplit(return_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password
            or parsed.path not in {"/inventory", "/stores"}
            or (parsed.fragment and (parsed.path != "/stores" or parsed.fragment != "stock-authorization"))):
        return JsonResponse({"error": "返回地址无效"}, status=400)
    allowed_origins = {
        origin.strip().rstrip("/") for origin in
        os.environ.get("STOCK_ERP_RETURN_ORIGINS", "http://localhost:3000").split(",") if origin.strip()
    }
    if f"{parsed.scheme}://{parsed.netloc}" not in allowed_origins:
        return JsonResponse({"error": "ERP 返回地址未获准"}, status=403)
    expected = hmac.new(key.encode(), f"{state}|{return_url}".encode(), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(expected, signature):
        return JsonResponse({"error": "绑定签名无效"}, status=403)
    code = secrets.token_urlsafe(32)
    ErpLinkGrant.objects.create(
        user=request.user, code_hash=hashlib.sha256(code.encode()).hexdigest(),
        state=state, expires_at=timezone.now() + timedelta(minutes=5),
    )
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"stock_state": state, "stock_code": code})
    return redirect(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)))


@csrf_exempt
@require_POST
def erp_link_exchange(request):
    key = os.environ.get("STOCK_ERP_API_KEY", "")
    if not key or not secrets.compare_digest(key, request.headers.get("X-Stock-Key", "")):
        return JsonResponse({"error": "无效的接口密钥"}, status=401)
    if len(request.body) > 4096:
        return JsonResponse({"error": "请求过大"}, status=413)
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "无效的JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "绑定凭据无效"}, status=400)
    code = str(payload.get("code") or "")
    state = str(payload.get("state") or "")
    if not code or not state:
        return JsonResponse({"error": "绑定凭据无效"}, status=400)
    with transaction.atomic():
        grant = ErpLinkGrant.objects.select_for_update().select_related("user").filter(
            code_hash=hashlib.sha256(code.encode()).hexdigest(), state=state,
            consumed_at__isnull=True, expires_at__gt=timezone.now(),
        ).first()
        if grant is None or not grant.user.is_active or not grant.user.is_staff:
            return JsonResponse({"error": "绑定凭据已失效"}, status=400)
        grant.consumed_at = timezone.now()
        grant.save(update_fields=["consumed_at"])
    return JsonResponse({"user_id": grant.user_id, "username": grant.user.get_username()})


@require_GET
def catalog_api(request):
    """Read-only ERP catalog, including items that currently have zero stock."""
    configured_key = os.environ.get("STOCK_ERP_API_KEY", "")
    if not configured_key:
        return JsonResponse({"error": "目录接口尚未配置"}, status=503)
    if not secrets.compare_digest(configured_key, request.headers.get("X-Stock-Key", "")):
        return JsonResponse({"error": "无效的接口密钥"}, status=401)
    stock_user_id = request.headers.get("X-Stock-User-Id", "")
    if not stock_user_id.isdecimal() or not get_user_model().objects.filter(
        pk=int(stock_user_id), is_active=True, is_staff=True,
    ).exists():
        return JsonResponse({"error": "Stock 账号绑定无效"}, status=403)
    balances = defaultdict(list)
    for balance in StockBalance.objects.select_related("warehouse").filter(warehouse__owner_id=int(stock_user_id)).order_by("warehouse__code"):
        balances[balance.item_id].append({
            "warehouse_code": balance.warehouse.code,
            "warehouse_name": balance.warehouse.name,
            "quantity": balance.on_hand,
        })
    return JsonResponse({"items": [
        {"sku": item.sku, "name": item.name, "warehouses": balances[item.id]}
        for item in StockItem.objects.filter(owner_id=int(stock_user_id)).order_by("sku")
    ]})


@csrf_exempt
@require_POST
def order_webhook(request):
    configured_key = os.environ.get("STOCK_ERP_API_KEY", "")
    if not configured_key:
        return JsonResponse({"error": "订单接口尚未配置"}, status=503)
    provided_key = request.headers.get("X-Stock-Key", "")
    if not secrets.compare_digest(configured_key, provided_key):
        return JsonResponse({"error": "无效的接口密钥"}, status=401)
    stock_user_id = request.headers.get("X-Stock-User-Id", "")
    if not stock_user_id.isdecimal() or not get_user_model().objects.filter(
        pk=int(stock_user_id), is_active=True, is_staff=True,
    ).exists():
        return JsonResponse({"error": "Stock 账号绑定无效"}, status=403)
    if len(request.body) > 1024 * 1024:
        return JsonResponse({"error": "订单数据过大"}, status=413)
    try:
        payload = json.loads(request.body)
        order, created = upsert_external_order(payload, owner_id=int(stock_user_id))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "无效的JSON"}, status=400)
    except StockError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"id": order.id, "created": created, "status": order.status})
