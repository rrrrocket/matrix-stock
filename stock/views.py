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
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone

from .models import ErpLinkGrant, ExternalOrder, OrderLine, StockBalance, StockItem, StockMovement, Warehouse
from .forms import StockRegistrationForm
from .services import StockError, adjust_stock, import_opening_stock, record_inbound, record_outbound, upsert_external_order
from .erp_callback import send_outbound_callback


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
        return redirect("dashboard")
    form = StockRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "注册申请已提交。请联系管理员审核账号，通过后即可登录。")
        return redirect("login")
    return render(request, "stock/register.html", {"form": form})


def _inventory_rows(query="", warehouse_id=""):
    balances = StockBalance.objects.select_related("warehouse", "item").filter(on_hand__gt=0)
    if query:
        balances = balances.filter(Q(item__sku__icontains=query) | Q(item__name__icontains=query))
    if warehouse_id:
        balances = balances.filter(warehouse_id=warehouse_id)
    return balances.order_by("item__sku", "warehouse__name")[:500]


def _order_rows(status_filter="open", query=""):
    lines = OrderLine.objects.select_related("order").filter(is_present=True)
    if status_filter == "open":
        lines = lines.filter(order__status=ExternalOrder.Status.OPEN, is_skipped=False, quantity__gt=0)
    elif status_filter == "closed":
        lines = lines.filter(Q(order__status__in=[ExternalOrder.Status.CANCELLED, ExternalOrder.Status.CLOSED]) | Q(is_skipped=True))
    if query:
        lines = lines.filter(Q(sku__icontains=query) | Q(order__external_order_id__icontains=query) | Q(order__store_name__icontains=query))
    lines = list(lines.order_by("-order__received_at", "-id")[:250])
    skus = {line.sku for line in lines}
    stock_by_sku = defaultdict(list)
    for balance in StockBalance.objects.select_related("warehouse", "item").filter(item__sku__in=skus, on_hand__gt=0, warehouse__is_active=True):
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
    rows = [line for line in _order_rows() if line.needs_action]
    context = {
        "active_nav": "dashboard",
        "warehouse_count": Warehouse.objects.filter(is_active=True).count(),
        "sku_count": StockItem.objects.count(),
        "stock_total": StockBalance.objects.aggregate(total=Sum("on_hand"))["total"] or 0,
        "pending_count": len(rows),
        "pending_rows": rows[:8],
        "recent_movements": StockMovement.objects.select_related("warehouse", "item", "operator")[:6],
    }
    return render(request, "stock/dashboard.html", context)


@staff_required
def warehouses(request):
    if request.method == "POST":
        code = str(request.POST.get("code") or "").strip()
        name = str(request.POST.get("name") or "").strip()
        if not code or not name or len(code) > 40 or len(name) > 100:
            messages.error(request, "请填写有效的仓库编码和名称")
        elif Warehouse.objects.filter(code=code).exists():
            messages.error(request, "仓库编码已存在")
        else:
            Warehouse.objects.create(code=code, name=name)
            messages.success(request, "仓库已创建")
        return redirect("warehouses")
    return render(request, "stock/warehouses.html", {
        "active_nav": "warehouses", "warehouses": Warehouse.objects.all(),
    })


@staff_required
@require_GET
def inventory(request):
    query = request.GET.get("q", "").strip()
    warehouse_id = request.GET.get("warehouse", "").strip()
    return render(request, "stock/inventory.html", {
        "active_nav": "inventory",
        "balances": _inventory_rows(query, warehouse_id),
        "warehouses": Warehouse.objects.filter(is_active=True),
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
            tracking_number=request.POST.get("tracking_number"),
            product_name=request.POST.get("product_name"),
            note=request.POST.get("note"),
            operator=request.user,
        )
        messages.success(request, f"{movement.item.sku} 已入库 {movement.quantity_change} 件")
    except (StockError, Warehouse.DoesNotExist) as exc:
        messages.error(request, str(exc) if isinstance(exc, StockError) else "仓库不存在")
    return redirect("inventory")


@staff_required
@require_POST
def import_opening(request):
    uploaded = request.FILES.get("file")
    if not uploaded:
        messages.error(request, "请选择期初库存文件")
        return redirect("inventory")
    try:
        count = import_opening_stock(uploaded, request.user)
        messages.success(request, f"期初库存导入成功，共 {count} 条。已有流水的仓库货号不会被重复导入。")
    except StockError as exc:
        messages.error(request, str(exc))
    return redirect("inventory")


@staff_required
@require_POST
def adjust(request):
    try:
        movement = adjust_stock(
            warehouse_id=request.POST.get("warehouse_id"),
            sku=request.POST.get("sku"),
            new_quantity=request.POST.get("new_quantity"),
            note=request.POST.get("note"),
            operator=request.user,
        )
        messages.success(request, f"{movement.item.sku} 已调整为 {movement.after_quantity} 件")
    except (StockError, Warehouse.DoesNotExist, StockItem.DoesNotExist) as exc:
        messages.error(request, str(exc) if isinstance(exc, StockError) else "仓库或货号不存在")
    return redirect("inventory")


@staff_required
@require_GET
def orders(request):
    status_filter = request.GET.get("status", "open")
    if status_filter not in {"open", "closed", "all"}:
        status_filter = "open"
    query = request.GET.get("q", "").strip()
    rows = _order_rows(status_filter, query)
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
            operation_id=request.POST.get("operation_id"),
            operator=request.user,
        )
        synced = send_outbound_callback(movement.id)
        messages.success(request, f"已从 {movement.warehouse.name} 出库 {-movement.quantity_change} 件；国内快递单号{'已回填 ERP' if synced else '待回填 ERP，将自动重试'}")
    except (StockError, Warehouse.DoesNotExist, OrderLine.DoesNotExist) as exc:
        messages.error(request, str(exc) if isinstance(exc, StockError) else "订单商品或仓库不存在")
    return redirect("orders")


@staff_required
@require_POST
def skip_order_line(request, line_id):
    line = get_object_or_404(OrderLine.objects.select_related("order"), pk=line_id)
    if line.order.status != ExternalOrder.Status.OPEN:
        messages.error(request, "订单已结束")
    else:
        line.is_skipped = True
        line.save(update_fields=["is_skipped"])
        messages.success(request, "已标记为不从自有仓出库；已出库记录不受影响")
    return redirect("orders")


@staff_required
@require_GET
def movements(request):
    query = request.GET.get("q", "").strip()
    rows = StockMovement.objects.select_related("warehouse", "item", "operator", "order_line__order")
    if query:
        rows = rows.filter(Q(item__sku__icontains=query) | Q(order_line__order__external_order_id__icontains=query))
    return render(request, "stock/movements.html", {
        "active_nav": "movements", "movements": rows[:300], "query": query,
    })


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
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.fragment or parsed.path != "/inventory":
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
    return redirect(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")))


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
    for balance in StockBalance.objects.select_related("warehouse").order_by("warehouse__code"):
        balances[balance.item_id].append({
            "warehouse_code": balance.warehouse.code,
            "warehouse_name": balance.warehouse.name,
            "quantity": balance.on_hand,
        })
    return JsonResponse({"items": [
        {"sku": item.sku, "name": item.name, "warehouses": balances[item.id]}
        for item in StockItem.objects.order_by("sku")
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
        order, created = upsert_external_order(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "无效的JSON"}, status=400)
    except StockError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"id": order.id, "created": created, "status": order.status})
