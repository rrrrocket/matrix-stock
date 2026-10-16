import time

from django.core.management.base import BaseCommand

from stock.notifications import dispatch_pending_alerts, dispatch_expiry_alerts, enqueue_expiry_alerts, expiry_send_date
from stock.erp_callback import dispatch_outbound_callbacks


class Command(BaseCommand):
    help = "发送订单和库存到期提醒，并重试 ERP 回填；默认持续轮询，--once 仅执行一轮"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=int, default=10)

    def handle(self, *args, **options):
        last_expiry_scan_date = None
        while True:
            send_date = expiry_send_date()
            if send_date is not None and last_expiry_scan_date != send_date:
                enqueue_expiry_alerts(send_date)
                last_expiry_scan_date = send_date
            sent = dispatch_pending_alerts()
            if sent:
                self.stdout.write(f"已发送 {sent} 条自有仓提醒")
            callbacks = dispatch_outbound_callbacks()
            if callbacks:
                self.stdout.write(f"已回填 {callbacks} 条出库快递单号")
            expiry_sent = dispatch_expiry_alerts() if send_date is not None else 0
            if expiry_sent:
                self.stdout.write(f"已发送 {expiry_sent} 条库存到期提醒")
            if options["once"]:
                break
            time.sleep(max(options["interval"], 1))
