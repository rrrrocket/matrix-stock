import time

from django.core.management.base import BaseCommand

from stock.notifications import dispatch_pending_alerts


class Command(BaseCommand):
    help = "发送自有仓订单提醒；默认持续轮询，--once 仅执行一轮"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=int, default=10)

    def handle(self, *args, **options):
        while True:
            sent = dispatch_pending_alerts()
            if sent:
                self.stdout.write(f"已发送 {sent} 条自有仓提醒")
            if options["once"]:
                break
            time.sleep(max(options["interval"], 1))
