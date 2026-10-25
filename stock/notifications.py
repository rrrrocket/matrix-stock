import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import timedelta
from urllib import error, request
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import OrderAlert, StockBalance, StockExpiryAlert, StockNotificationSettings
from .services import matching_order_lines


def parse_expiry_reminder_days(value):
    parts = re.split(r"[,，、/\s]+", str(value or "").replace("天", "").strip())
    if not 1 <= len(parts) <= 12 or any(not part.isdecimal() or len(part) > 3 for part in parts):
        raise ValueError("请输入 1 至 12 个提醒天数，例如 7,3,1")
    days = sorted({int(part) for part in parts}, reverse=True)
    if any(day > 365 for day in days):
        raise ValueError("提醒天数须在 0 至 365 天之间")
    return tuple(days)


def _active_expiry_stage(expires_on, today, stages):
    remaining = (expires_on - today).days
    return next((day for day in reversed(stages) if remaining <= day), None)


def expiry_send_date(now=None):
    local_now = timezone.localtime(now) if now is not None else timezone.localtime()
    return local_now.date() if local_now.hour >= 21 else None


def _clean(value):
    return str(value or "").replace("\n", " ").replace("\r", " ")[:150]


def _message_for_order(order):
    matched = matching_order_lines(order)
    if not matched:
        return None
    lines = [f"店铺：{_clean(order.store_name)}\n订单：{_clean(order.external_order_id)}（{_clean(order.platform)}）"]
    for line, total in matched[:20]:
        stock = StockBalance.objects.select_related("warehouse").filter(
            warehouse__owner=order.owner, item__sku=line.sku, on_hand__gt=0, warehouse__is_active=True,
        ).order_by("warehouse__name")
        warehouses = "、".join(f"{_clean(row.warehouse.name)} {row.on_hand}" for row in stock)
        lines.append(f"{_clean(line.sku)}：本单待出库 {line.remaining_quantity}，自有仓合计 {total}（{warehouses}）")
    if len(matched) > 20:
        lines.append(f"另有 {len(matched) - 20} 个货号，请进入库存系统查看")
    lines.append("请优先核对自有仓；只有确认出库后才扣减库存。")
    return "\n".join(lines)


def _send_feishu(content, *, webhook=None, secret=None, title="自有仓优先出库提醒"):
    explicit_webhook = webhook is not None
    webhook = webhook if webhook is not None else os.environ.get("STOCK_FEISHU_WEBHOOK", "")
    if urlsplit(webhook).scheme != "https" or (explicit_webhook and urlsplit(webhook).hostname not in {"open.feishu.cn", "open.larksuite.com"}):
        raise ValueError("未配置有效的 STOCK_FEISHU_WEBHOOK")
    body = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
        },
    }
    secret = secret if secret is not None else os.environ.get("STOCK_FEISHU_SECRET", "")
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


def _send_wecom(content, webhook):
    parsed = urlsplit(webhook)
    if parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com":
        raise ValueError("未配置有效的企业微信机器人地址")
    payload = json.dumps({"msgtype": "text", "text": {"content": content}}, ensure_ascii=False).encode()
    req = request.Request(webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=8) as response:
            response_data = json.loads(response.read() or b"{}")
    except (error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"企业微信请求失败：{exc}") from exc
    if response_data.get("errcode") != 0:
        raise RuntimeError(f"企业微信拒绝通知：{response_data.get('errmsg') or response_data}")


def enqueue_expiry_alerts(today=None):
    today = today or timezone.localdate()
    balances = StockBalance.objects.select_related("warehouse", "warehouse__owner", "item", "warehouse__owner__stock_notification_settings").filter(
        on_hand__gt=0, expires_on__isnull=False, expires_on__lte=today + timedelta(days=365),
        warehouse__is_active=True, warehouse__owner__stock_notification_settings__isnull=False,
    )
    queued = 0
    for balance in balances.iterator():
        settings_row = balance.warehouse.owner.stock_notification_settings
        stage = _active_expiry_stage(balance.expires_on, today, parse_expiry_reminder_days(settings_row.expiry_reminder_days))
        if stage is None:
            continue
        for channel, webhook in ((StockExpiryAlert.Channel.FEISHU, settings_row.feishu_webhook),
                                 (StockExpiryAlert.Channel.WECOM, settings_row.wecom_webhook)):
            if webhook:
                _, created = StockExpiryAlert.objects.get_or_create(
                    balance=balance, expires_on=balance.expires_on, channel=channel, reminder_days=stage,
                )
                queued += created
    return queued


def dispatch_expiry_alerts(limit=20):
    if expiry_send_date() is None:
        return 0
    now = timezone.now()
    candidates = list(StockExpiryAlert.objects.filter(
        Q(status=StockExpiryAlert.Status.PENDING, next_attempt_at__isnull=True)
        | Q(status=StockExpiryAlert.Status.PENDING, next_attempt_at__lte=now)
        | Q(status=StockExpiryAlert.Status.SENDING, locked_at__lte=now - timedelta(minutes=5))
    ).order_by("expires_on", "pk").values_list("pk", flat=True)[:limit])
    sent = 0
    for alert_id in candidates:
        with transaction.atomic():
            # The owner relation is nullable; PostgreSQL can only lock the alert row here.
            alert = StockExpiryAlert.objects.select_related("balance__warehouse__owner", "balance__item").select_for_update(of=("self",)).get(pk=alert_id)
            if alert.status == StockExpiryAlert.Status.SENT:
                continue
            if alert.status == StockExpiryAlert.Status.SENDING and alert.locked_at and alert.locked_at > timezone.now() - timedelta(minutes=5):
                continue
            balance = alert.balance
            settings_row = StockNotificationSettings.objects.filter(owner=balance.warehouse.owner).first()
            webhook = (settings_row.feishu_webhook if alert.channel == StockExpiryAlert.Channel.FEISHU
                       else settings_row.wecom_webhook) if settings_row else ""
            active_stage = (_active_expiry_stage(alert.expires_on, timezone.localdate(),
                            parse_expiry_reminder_days(settings_row.expiry_reminder_days)) if settings_row else None)
            if (balance.on_hand == 0 or not balance.warehouse.is_active or balance.expires_on != alert.expires_on
                    or not webhook or alert.reminder_days != active_stage):
                alert.delete()
                continue
            alert.status = StockExpiryAlert.Status.SENDING
            alert.locked_at = timezone.now()
            alert.attempts += 1
            alert.save(update_fields=["status", "locked_at", "attempts"])
        days = (alert.expires_on - timezone.localdate()).days
        remaining = f"还有 {days} 天" if days >= 0 else f"已过期 {-days} 天"
        content = (f"库存到期提醒（提前 {alert.reminder_days} 天）\n仓库：{_clean(balance.warehouse.name)}\n货号：{_clean(balance.item.sku)}\n"
                   f"库存：{balance.on_hand} 件\n过期时间：{alert.expires_on.isoformat()}（{remaining}）")
        try:
            if alert.channel == StockExpiryAlert.Channel.FEISHU:
                _send_feishu(content, webhook=webhook, secret=settings_row.feishu_secret, title="库存到期提醒")
            else:
                _send_wecom(content, webhook)
        except Exception as exc:
            StockExpiryAlert.objects.filter(pk=alert_id, status=StockExpiryAlert.Status.SENDING).update(
                status=StockExpiryAlert.Status.PENDING, last_error=str(exc)[:500],
                next_attempt_at=timezone.now() + timedelta(minutes=min(2 ** min(alert.attempts, 6), 60)),
                locked_at=None,
            )
        else:
            sent += StockExpiryAlert.objects.filter(pk=alert_id, status=StockExpiryAlert.Status.SENDING).update(
                status=StockExpiryAlert.Status.SENT, sent_at=timezone.now(), last_error="", locked_at=None,
            )
    return sent


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
