import json
import os
import uuid
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from .models import ExternalOrder, OrderAlert, OrderLine, StockBalance, StockItem, StockMovement, Warehouse
from .notifications import dispatch_pending_alerts
from .services import StockError, import_opening_stock, record_inbound, record_outbound, upsert_external_order


class StockFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="warehouse", password="test-password", is_staff=True)
        self.a = Warehouse.objects.create(code="A", name="上海仓")
        self.b = Warehouse.objects.create(code="B", name="义乌仓")
        record_inbound(warehouse_id=self.a.id, sku="SKU-1", quantity=3, operator=self.user)
        record_inbound(warehouse_id=self.b.id, sku="SKU-1", quantity=4, operator=self.user)
        self.payload = {
            "platform": "ozon", "store_id": "18", "store_name": "测试店铺",
            "order_id": "posting-1", "status": "open",
            "items": [{"sku": "SKU-1", "name": "测试商品", "quantity": 5}],
        }

    def test_multiple_warehouses_can_fulfill_one_order_without_double_deducting(self):
        order, created = upsert_external_order(self.payload)
        self.assertTrue(created)
        line = order.lines.get()
        first_id = uuid.uuid4()
        first = record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=3, operator=self.user, operation_id=first_id)
        duplicate = record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=3, operator=self.user, operation_id=first_id)
        self.assertEqual(first.id, duplicate.id)
        with self.assertRaises(StockError):
            record_outbound(line_id=line.id, warehouse_id=self.b.id, quantity=1, operator=self.user, operation_id=first_id)
        record_outbound(line_id=line.id, warehouse_id=self.b.id, quantity=2, operator=self.user, operation_id=uuid.uuid4())
        line.refresh_from_db()
        self.assertEqual(line.remaining_quantity, 0)
        self.assertEqual(StockBalance.objects.get(warehouse=self.a, item__sku="SKU-1").on_hand, 0)
        self.assertEqual(StockBalance.objects.get(warehouse=self.b, item__sku="SKU-1").on_hand, 2)
        self.assertEqual(StockMovement.objects.filter(movement_type=StockMovement.Type.OUTBOUND).count(), 2)
        with self.assertRaises(StockError):
            record_outbound(line_id=line.id, warehouse_id=self.b.id, quantity=1, operator=self.user, operation_id=uuid.uuid4())

    def test_order_resync_does_not_reset_outbound_and_cancel_does_not_restore_stock(self):
        order, _ = upsert_external_order(self.payload)
        line = order.lines.get()
        record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=2, operator=self.user, operation_id=uuid.uuid4())
        _, created = upsert_external_order(self.payload)
        self.assertFalse(created)
        line.refresh_from_db()
        self.assertEqual(line.outbound_quantity, 2)
        self.payload["status"] = "cancelled"
        upsert_external_order(self.payload)
        with self.assertRaises(StockError):
            record_outbound(line_id=line.id, warehouse_id=self.b.id, quantity=1, operator=self.user, operation_id=uuid.uuid4())
        self.assertEqual(StockBalance.objects.get(warehouse=self.a, item__sku="SKU-1").on_hand, 1)

    def test_failed_outbound_rolls_back_all_changes(self):
        order, _ = upsert_external_order(self.payload)
        line = order.lines.get()
        with self.assertRaises(StockError):
            record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=4, operator=self.user, operation_id=uuid.uuid4())
        line.refresh_from_db()
        self.assertEqual(line.outbound_quantity, 0)
        self.assertEqual(StockBalance.objects.get(warehouse=self.a, item__sku="SKU-1").on_hand, 3)

    def test_same_order_id_from_different_stores_is_distinct(self):
        first, _ = upsert_external_order(self.payload)
        self.payload["store_id"] = "19"
        second, _ = upsert_external_order(self.payload)
        self.assertNotEqual(first.id, second.id)

    def test_duplicate_sku_rows_are_aggregated(self):
        self.payload["items"].append({"sku": "SKU-1", "quantity": 2})
        order, _ = upsert_external_order(self.payload)
        self.assertEqual(order.lines.get().quantity, 7)

    def test_erp_space_and_dash_skus_remain_distinct(self):
        record_inbound(warehouse_id=self.a.id, sku="品牌 型号", quantity=1, operator=self.user)
        record_inbound(warehouse_id=self.a.id, sku="品牌-型号", quantity=1, operator=self.user)
        self.assertTrue(StockItem.objects.filter(sku="品牌 型号").exists())
        self.assertTrue(StockItem.objects.filter(sku="品牌-型号").exists())

    def test_versioned_snapshot_rejects_stale_changes_and_preserves_outbound(self):
        self.payload["version"] = 1
        order, _ = upsert_external_order(self.payload)
        line = order.lines.get()
        record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=2, operator=self.user, operation_id=uuid.uuid4())
        self.payload["version"] = 2
        self.payload["status"] = "cancelled"
        upsert_external_order(self.payload)
        self.payload["version"] = 1
        self.payload["status"] = "open"
        with self.assertRaises(StockError):
            upsert_external_order(self.payload)
        order.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(order.snapshot_version, 2)
        self.assertEqual(line.outbound_quantity, 2)

    def test_webhook_requires_configured_key(self):
        with patch.dict(os.environ, {"STOCK_ERP_API_KEY": "test-key"}):
            response = self.client.post(reverse("order_webhook"), data=json.dumps(self.payload), content_type="application/json")
            self.assertEqual(response.status_code, 401)
            response = self.client.post(
                reverse("order_webhook"), data=json.dumps(self.payload), content_type="application/json",
                HTTP_X_STOCK_KEY="test-key",
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(ExternalOrder.objects.count(), 1)

    def test_matched_order_queues_one_alert_and_repeated_sync_does_not_duplicate_it(self):
        order, _ = upsert_external_order(self.payload)
        self.assertEqual(OrderAlert.objects.filter(order=order).count(), 1)
        with patch.dict(os.environ, {"STOCK_FEISHU_WEBHOOK": "https://example.test/bot"}):
            with patch("stock.notifications._send_feishu") as send:
                self.assertEqual(dispatch_pending_alerts(), 1)
                self.assertEqual(dispatch_pending_alerts(), 0)
                self.assertEqual(send.call_count, 1)
        upsert_external_order(self.payload)
        self.assertEqual(OrderAlert.objects.filter(order=order).count(), 1)
        self.assertEqual(OrderAlert.objects.get(order=order).status, OrderAlert.Status.SENT)

    def test_opening_import_accepts_excel_and_refuses_duplicate(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["仓库编码", "货号", "数量", "商品名称"])
        sheet.append(["A", "SKU-NEW", 6, "新商品"])
        output = BytesIO()
        workbook.save(output)
        data = output.getvalue()
        upload = SimpleUploadedFile("opening.xlsx", data)
        self.assertEqual(import_opening_stock(upload, self.user), 1)
        self.assertEqual(StockBalance.objects.get(warehouse=self.a, item__sku="SKU-NEW").on_hand, 6)
        with self.assertRaises(StockError):
            import_opening_stock(SimpleUploadedFile("opening.xlsx", data), self.user)
        self.assertEqual(StockBalance.objects.get(warehouse=self.a, item__sku="SKU-NEW").on_hand, 6)

    def test_alert_failure_remains_pending_for_retry(self):
        order, _ = upsert_external_order(self.payload)
        with patch.dict(os.environ, {"STOCK_FEISHU_WEBHOOK": "https://example.test/bot"}):
            with patch("stock.notifications._send_feishu", side_effect=RuntimeError("暂时失败")):
                self.assertEqual(dispatch_pending_alerts(), 0)
        alert = OrderAlert.objects.get(order=order)
        self.assertEqual(alert.status, OrderAlert.Status.PENDING)
        self.assertEqual(alert.attempts, 1)
        self.assertIsNotNone(alert.next_attempt_at)

    def test_pages_require_staff_login(self):
        self.assertEqual(self.client.get(reverse("inventory")).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("inventory")).status_code, 200)
        self.assertEqual(self.client.get(reverse("orders")).status_code, 200)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
