import base64
import hashlib
import hmac
import json
import os
import time
from datetime import timedelta
from urllib import error, request

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import OrderAlert, StockBalance
from .services import matching_order_lines


def _clean(value):
    return str(value or "").replace("\n", " ").replace("\r", " ")[:150]


def _message_for_order(order):
    matched = matching_order_lines(order)
    if not matched:
        return None
    lines = [f"店铺：{_clean(order.store_name)}\n订单：{_clean(order.external_order_id)}（{_clean(order.platform)}）"]
    for line, total in matched[:20]:
        stock = StockBalance.objects.select_related("warehouse").filter(
            item__sku=line.sku, on_hand__gt=0, warehouse__is_active=True,
        ).order_by("warehouse__name")
        warehouses = "、".join(f"{_clean(row.warehouse.name)} {row.on_hand}" for row in stock)
        lines.append(f"{_clean(line.sku)}：本单待出库 {line.remaining_quantity}，自有仓合计 {total}（{warehouses}）")
    if len(matched) > 20:
        lines.append(f"另有 {len(matched) - 20} 个货号，请进入库存系统查看")
    lines.append("请优先核对自有仓；只有确认出库后才扣减库存。")
    return "\n".join(lines)


def _send_feishu(content):
    webhook = os.environ.get("STOCK_FEISHU_WEBHOOK", "")
    if not webhook.startswith("https://"):
        raise ValueError("未配置有效的 STOCK_FEISHU_WEBHOOK")
    body = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": "自有仓优先出库提醒"}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
        },
    }
    secret = os.environ.get("STOCK_FEISHU_SECRET", "")
    if secret:
        timestamp = str(int(time.time()))
        sign = hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
        body.update({"timestamp": timestamp, "sign": base64.b64encode(sign).decode()})
    payload = json.dumps(body, ensure_ascii=False).encode()
    req = request.Request(webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=8) as response:
            response_data = json.loads(response.read() or b"{}")
    except (error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"飞书请求失败：{exc}") from exc
    if response_data.get("code", 0) not in (0, None):
        raise RuntimeError(f"飞书拒绝通知：{response_data.get('msg') or response_data}")


def dispatch_pending_alerts(limit=20):
    if not os.environ.get("STOCK_FEISHU_WEBHOOK"):
        return 0
    now = timezone.now()
    candidates = list(OrderAlert.objects.filter(
        Q(status=OrderAlert.Status.PENDING, next_attempt_at__isnull=True)
        | Q(status=OrderAlert.Status.PENDING, next_attempt_at__lte=now)
        | Q(status=OrderAlert.Status.SENDING, locked_at__lte=now - timedelta(minutes=5))
    ).values_list("pk", flat=True)[:limit])
    dispatched = 0
    for alert_id in candidates:
        with transaction.atomic():
            alert = OrderAlert.objects.select_for_update().select_related("order").get(pk=alert_id)
            if alert.status == OrderAlert.Status.SENT or alert.status == OrderAlert.Status.SKIPPED:
                continue
            if alert.status == OrderAlert.Status.SENDING and alert.locked_at and alert.locked_at > timezone.now() - timedelta(minutes=5):
                continue
            alert.status = OrderAlert.Status.SENDING
            alert.locked_at = timezone.now()
            alert.attempts += 1
            alert.save(update_fields=["status", "locked_at", "attempts"])
        content = _message_for_order(alert.order)
        if not content:
            alert.status = OrderAlert.Status.SKIPPED
            alert.locked_at = None
            alert.save(update_fields=["status", "locked_at"])
            continue
        try:
            _send_feishu(content)
        except Exception as exc:
            alert.status = OrderAlert.Status.PENDING
            alert.last_error = str(exc)[:500]
            alert.next_attempt_at = timezone.now() + timedelta(minutes=min(2 ** min(alert.attempts, 6), 60))
            alert.locked_at = None
            alert.save(update_fields=["status", "last_error", "next_attempt_at", "locked_at"])
        else:
            alert.status = OrderAlert.Status.SENT
            alert.sent_at = timezone.now()
            alert.last_error = ""
            alert.locked_at = None
            alert.save(update_fields=["status", "sent_at", "last_error", "locked_at"])
            dispatched += 1
    return dispatched
