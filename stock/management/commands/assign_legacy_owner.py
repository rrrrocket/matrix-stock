"""Assign legacy records whose owner could not be inferred safely."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stock.models import ExternalOrder, StockBalance, StockItem, StockMovement, Warehouse


class Command(BaseCommand):
    help = "List or assign legacy warehouses and orders without an owner."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="List records needing an owner")
        parser.add_argument("--user", help="Username receiving the record")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--warehouse", type=int, help="Unowned warehouse ID")
        group.add_argument("--order", type=int, help="Unowned order ID")
        group.add_argument("--item", type=int, help="Unowned catalog item ID without stock records")

    def handle(self, *args, **options):
        if options["list"]:
            for warehouse in Warehouse.objects.filter(owner__isnull=True).order_by("pk"):
                self.stdout.write(f"warehouse {warehouse.pk}: {warehouse.code} / {warehouse.name}")
            for order in ExternalOrder.objects.filter(owner__isnull=True).order_by("pk"):
                self.stdout.write(f"order {order.pk}: {order.platform} / {order.store_external_id} / {order.external_order_id}")
            for item in StockItem.objects.filter(owner__isnull=True).order_by("pk"):
                self.stdout.write(f"unassigned item {item.pk}: {item.sku}")
            return
        if not options["user"] or not (options["warehouse"] or options["order"] or options["item"]):
            raise CommandError("请使用 --list，或指定 --user 以及 --warehouse/--order/--item")
        user = get_user_model().objects.filter(username=options["user"], is_active=True, is_staff=True).first()
        if not user:
            raise CommandError("目标账号不存在或尚未获准使用库存系统")
        with transaction.atomic():
            if options["warehouse"]:
                warehouse = Warehouse.objects.select_for_update().filter(pk=options["warehouse"], owner__isnull=True).first()
                if not warehouse:
                    raise CommandError("仓库不存在或已经有归属账号")
                if Warehouse.objects.filter(owner=user, code=warehouse.code).exists():
                    raise CommandError("目标账号已有相同仓库编码")
                item_ids = set(StockBalance.objects.filter(warehouse=warehouse).values_list("item_id", flat=True))
                item_ids.update(StockMovement.objects.filter(warehouse=warehouse).values_list("item_id", flat=True))
                for item in StockItem.objects.filter(pk__in=item_ids):
                    target, _ = StockItem.objects.get_or_create(owner=user, sku=item.sku, defaults={"name": item.name})
                    StockBalance.objects.filter(warehouse=warehouse, item=item).update(item=target)
                    StockMovement.objects.filter(warehouse=warehouse, item=item).update(item=target)
                warehouse.owner = user
                warehouse.save(update_fields=["owner"])
                self.stdout.write(self.style.SUCCESS(f"仓库 {warehouse.pk} 已归属 {user.username}"))
            elif options["order"]:
                order = ExternalOrder.objects.select_for_update().filter(pk=options["order"], owner__isnull=True).first()
                if not order:
                    raise CommandError("订单不存在或已经有归属账号")
                if ExternalOrder.objects.filter(
                    owner=user, platform=order.platform, store_external_id=order.store_external_id,
                    external_order_id=order.external_order_id,
                ).exists():
                    raise CommandError("目标账号已有相同订单")
                order.owner = user
                order.save(update_fields=["owner"])
                self.stdout.write(self.style.SUCCESS(f"订单 {order.pk} 已归属 {user.username}"))
            else:
                item = StockItem.objects.select_for_update().filter(pk=options["item"], owner__isnull=True).first()
                if not item:
                    raise CommandError("货号不存在或已经有归属账号")
                if item.balances.exists() or item.movements.exists():
                    raise CommandError("货号仍关联库存记录；请先分配对应仓库")
                if StockItem.objects.filter(owner=user, sku=item.sku).exists():
                    raise CommandError("目标账号已有相同货号")
                item.owner = user
                item.save(update_fields=["owner"])
                self.stdout.write(self.style.SUCCESS(f"货号 {item.pk} 已归属 {user.username}"))
