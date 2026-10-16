"""Durable delivery of confirmed warehouse outbound tracking numbers to ERP."""

import json
import os
from datetime import timedelta
from urllib.error import HTTPError
from urllib import request

from django.db.models import Q
from django.utils import timezone

from .models import StockMovement


def send_outbound_callback(movement_id, *, force=False):
    movement = StockMovement.objects.select_related('order_line__order').get(pk=movement_id)
    if movement.erp_callback_sent_at and not force:
        return True
    if movement.movement_type != StockMovement.Type.OUTBOUND or not movement.order_line_id or not movement.tracking_number:
        return False
    url = os.environ.get('STOCK_ERP_CALLBACK_URL', '').strip()
    key = os.environ.get('STOCK_ERP_API_KEY', '')
    try:
        if not url.startswith(('http://', 'https://')) or not key:
            raise ValueError('ERP 回填地址或接口密钥未配置')
        order = movement.order_line.order
        payload = {
            'platform': order.platform,
            'store_id': order.store_external_id,
            'order_id': order.external_order_id,
            'operation_id': str(movement.operation_id),
            'warehouse_sku': movement.order_line.sku,
            'quantity': -movement.quantity_change,
            'tracking_number': movement.tracking_number,
        }
        outgoing = request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'X-Stock-Key': key,
                     'X-Stock-User-Id': str(order.owner_id)},
        )
        with request.urlopen(outgoing, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f'ERP 回填返回 HTTP {response.status}')
            body = response.read(4097)
            if len(body) > 4096:
                raise RuntimeError('ERP 回填响应过大，无法确认结果')
            try:
                confirmation = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError('ERP 回填未返回有效 JSON 确认') from None
            if (not isinstance(confirmation, dict) or type(confirmation.get('id')) is not int
                    or confirmation['id'] <= 0 or type(confirmation.get('created')) is not bool):
                raise RuntimeError('ERP 回填响应缺少有效确认')
    except (ValueError, OSError, RuntimeError) as exc:
        error = str(exc)
        if isinstance(exc, HTTPError):
            try:
                body = exc.read(1024).decode('utf-8', errors='replace')
                detail = json.loads(body).get('detail')
                if detail:
                    error = f'ERP 回填 HTTP {exc.code}: {detail}'
            except (ValueError, AttributeError, OSError):
                pass
        attempts = movement.erp_callback_attempts + 1
        movement.erp_callback_attempts = attempts
        movement.erp_callback_sent_at = None
        movement.erp_callback_next_attempt_at = timezone.now() + timedelta(seconds=min(30 * 2 ** min(attempts, 7), 3600))
        movement.erp_callback_error = error[:500]
        movement.save(update_fields=['erp_callback_sent_at', 'erp_callback_attempts', 'erp_callback_next_attempt_at', 'erp_callback_error'])
        return False
    movement.erp_callback_sent_at = timezone.now()
    movement.erp_callback_next_attempt_at = None
    movement.erp_callback_error = ''
    movement.save(update_fields=['erp_callback_sent_at', 'erp_callback_next_attempt_at', 'erp_callback_error'])
    return True


def dispatch_outbound_callbacks(limit=20):
    now = timezone.now()
    ids = list(StockMovement.objects.filter(
        movement_type=StockMovement.Type.OUTBOUND,
        tracking_number__gt='',
        erp_callback_sent_at__isnull=True,
    ).filter(Q(erp_callback_next_attempt_at__isnull=True) | Q(erp_callback_next_attempt_at__lte=now))
        .order_by('id').values_list('id', flat=True)[:limit])
    return sum(send_outbound_callback(movement_id) for movement_id in ids)
