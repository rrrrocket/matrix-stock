import json
import hashlib
import hmac
import os
import uuid
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from .models import ExternalOrder, OrderAlert, OrderLine, StockBalance, StockItem, StockMovement, Warehouse
from .notifications import dispatch_pending_alerts
from .services import StockError, import_opening_stock, record_inbound, record_outbound, upsert_external_order
from .erp_callback import send_outbound_callback


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

    @patch.dict(os.environ, {"STOCK_ERP_API_KEY": "test-key"})
    def test_catalog_api_requires_key_and_lists_zero_stock_items(self):
        StockItem.objects.create(sku="ZERO-1", name="无库存商品")
        self.assertEqual(self.client.get(reverse("catalog_api")).status_code, 401)
        self.assertEqual(self.client.get(reverse("catalog_api"), HTTP_X_STOCK_KEY="test-key").status_code, 403)
        response = self.client.get(reverse("catalog_api"), HTTP_X_STOCK_KEY="test-key", HTTP_X_STOCK_USER_ID=str(self.user.pk))
        self.assertEqual(response.status_code, 200)
        items = {item["sku"]: item for item in response.json()["items"]}
        self.assertEqual(items["ZERO-1"]["warehouses"], [])
        self.assertEqual(len(items["SKU-1"]["warehouses"]), 2)

    @patch.dict(os.environ, {"STOCK_ERP_API_KEY": "test-key"})
    def test_erp_login_grant_is_signed_and_single_use(self):
        state = "erp-signed-state"
        return_url = "http://localhost:3000/inventory?tab=warehouse"
        signature = hmac.new(b"test-key", f"{state}|{return_url}".encode(), hashlib.sha256).hexdigest()
        connect_url = reverse("erp_connect")
        params = {"state": state, "return_url": return_url, "signature": signature}
        login_redirect = self.client.get(connect_url, params)
        self.assertEqual(login_redirect.status_code, 302)
        login_response = self.client.post(login_redirect["Location"], {
            "username": "warehouse", "password": "test-password",
        })
        self.assertEqual(login_response.status_code, 302)
        self.assertIn("/erp/connect/", login_response["Location"])
        bad = self.client.get(connect_url, {**params, "signature": "wrong"})
        self.assertEqual(bad.status_code, 403)
        bad_origin = "https://attacker.example/inventory?tab=warehouse"
        bad_signature = hmac.new(b"test-key", f"{state}|{bad_origin}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(self.client.get(connect_url, {**params, "return_url": bad_origin, "signature": bad_signature}).status_code, 403)
        response = self.client.get(connect_url, params)
        self.assertEqual(response.status_code, 302)
        callback = urlsplit(response["Location"])
        self.assertEqual(callback.netloc, "localhost:3000")
        code = parse_qs(callback.query)["stock_code"][0]
        payload = json.dumps({"state": state, "code": code})
        exchange_url = reverse("erp_link_exchange")
        first = self.client.post(exchange_url, payload, content_type="application/json", HTTP_X_STOCK_KEY="test-key")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["user_id"], self.user.pk)
        self.assertEqual(self.client.post(exchange_url, payload, content_type="application/json", HTTP_X_STOCK_KEY="test-key").status_code, 400)

    def test_multiple_warehouses_can_fulfill_one_order_without_double_deducting(self):
        order, created = upsert_external_order(self.payload)
        self.assertTrue(created)
        line = order.lines.get()
        first_id = uuid.uuid4()
        first = record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=3, tracking_number='YT123', operator=self.user, operation_id=first_id)
        duplicate = record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=3, tracking_number='YT123', operator=self.user, operation_id=first_id)
        self.assertEqual(first.id, duplicate.id)
        with self.assertRaises(StockError):
            record_outbound(line_id=line.id, warehouse_id=self.b.id, quantity=1, tracking_number='YT123', operator=self.user, operation_id=first_id)
        record_outbound(line_id=line.id, warehouse_id=self.b.id, quantity=2, tracking_number='YT124', operator=self.user, operation_id=uuid.uuid4())
        line.refresh_from_db()
        self.assertEqual(line.remaining_quantity, 0)
        self.assertEqual(StockBalance.objects.get(warehouse=self.a, item__sku="SKU-1").on_hand, 0)
        self.assertEqual(StockBalance.objects.get(warehouse=self.b, item__sku="SKU-1").on_hand, 2)
        self.assertEqual(StockMovement.objects.filter(movement_type=StockMovement.Type.OUTBOUND).count(), 2)
        with self.assertRaises(StockError):
            record_outbound(line_id=line.id, warehouse_id=self.b.id, quantity=1, tracking_number='YT125', operator=self.user, operation_id=uuid.uuid4())

    def test_outbound_requires_real_tracking_and_retries_erp_callback(self):
        order, _ = upsert_external_order(self.payload)
        line = order.lines.get()
        with self.assertRaises(StockError):
            record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=1, tracking_number='', operator=self.user, operation_id=uuid.uuid4())
        movement = record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=1, tracking_number='YT123', operator=self.user, operation_id=uuid.uuid4())
        with patch.dict(os.environ, {'STOCK_ERP_CALLBACK_URL': 'http://erp.test/api/orders/stock-outbound', 'STOCK_ERP_API_KEY': 'test-key'}):
            with patch('stock.erp_callback.request.urlopen', side_effect=OSError('offline')):
                self.assertFalse(send_outbound_callback(movement.id))
            movement.refresh_from_db()
            self.assertIsNone(movement.erp_callback_sent_at)
            self.assertEqual(movement.erp_callback_attempts, 1)
            response = MagicMock()
            response.__enter__.return_value.status = 200
            with patch('stock.erp_callback.request.urlopen', return_value=response) as outgoing:
                self.assertTrue(send_outbound_callback(movement.id))
                payload = json.loads(outgoing.call_args.args[0].data)
                self.assertEqual(payload['tracking_number'], 'YT123')
                self.assertEqual(payload['operation_id'], str(movement.operation_id))
            self.assertTrue(send_outbound_callback(movement.id))
            self.assertEqual(outgoing.call_count, 1)

    def test_order_resync_does_not_reset_outbound_and_cancel_does_not_restore_stock(self):
        order, _ = upsert_external_order(self.payload)
        line = order.lines.get()
        record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=2, tracking_number='YT123', operator=self.user, operation_id=uuid.uuid4())
        _, created = upsert_external_order(self.payload)
        self.assertFalse(created)
        line.refresh_from_db()
        self.assertEqual(line.outbound_quantity, 2)
        self.payload["status"] = "cancelled"
        upsert_external_order(self.payload)
        with self.assertRaises(StockError):
            record_outbound(line_id=line.id, warehouse_id=self.b.id, quantity=1, tracking_number='YT124', operator=self.user, operation_id=uuid.uuid4())
        self.assertEqual(StockBalance.objects.get(warehouse=self.a, item__sku="SKU-1").on_hand, 1)

    def test_failed_outbound_rolls_back_all_changes(self):
        order, _ = upsert_external_order(self.payload)
        line = order.lines.get()
        with self.assertRaises(StockError):
            record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=4, tracking_number='YT123', operator=self.user, operation_id=uuid.uuid4())
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
        record_outbound(line_id=line.id, warehouse_id=self.a.id, quantity=2, tracking_number='YT123', operator=self.user, operation_id=uuid.uuid4())
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
            self.assertEqual(response.status_code, 403)
            response = self.client.post(
                reverse("order_webhook"), data=json.dumps(self.payload), content_type="application/json",
                HTTP_X_STOCK_KEY="test-key", HTTP_X_STOCK_USER_ID=str(self.user.pk),
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

    def test_registration_requires_staff_approval_before_login(self):
        self.assertContains(self.client.get(reverse("login")), "申请注册")
        response = self.client.post(reverse("register"), {
            "username": "new-operator", "password1": "WarehousePass-2026!", "password2": "WarehousePass-2026!",
        })
        self.assertRedirects(response, reverse("login"))
        registered = get_user_model().objects.get(username="new-operator")
        self.assertFalse(registered.is_staff)
        response = self.client.post(reverse("login"), {
            "username": "new-operator", "password": "WarehousePass-2026!",
        })
        self.assertContains(response, "等待管理员审核")
        registered.is_staff = True
        registered.save(update_fields=["is_staff"])
        response = self.client.post(reverse("login"), {
            "username": "new-operator", "password": "WarehousePass-2026!",
        })
        self.assertRedirects(response, reverse("dashboard"))


class AdminReviewTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(username="admin-review", password="admin-test-password")
        self.pending = get_user_model().objects.create_user(username="pending-review", password="operator-test-password")
        self.url = reverse("stock_admin_users")

    def test_review_and_user_management_page_requires_user_admin_permission(self):
        self.assertRedirects(self.client.get(self.url), f"/admin/login/?next={self.url}")
        regular_member = get_user_model().objects.create_user(username="member", password="member-test-password", is_staff=True)
        self.client.force_login(regular_member)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url)
        self.assertContains(response, "待审核注册")
        self.assertContains(response, "全部用户")
        self.assertContains(response, "pending-review")
        self.assertContains(response, "stock/admin_users.css")
        self.assertNotContains(response, "仓库与订单")
        self.assertEqual(self.client.get("/admin/auth/user/").status_code, 404)
        self.assertEqual(self.client.get("/admin/stock/warehouse/").status_code, 404)

    def test_approve_and_disable_user_access(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self.url, {"action": "approve", "user_id": self.pending.pk})
        self.assertRedirects(response, f"{self.url}#pending")
        self.pending.refresh_from_db()
        self.assertTrue(self.pending.is_staff)
        self.assertEqual(self.client.get(self.url).context["pending_page"].paginator.count, 0)

        self.client.post(self.url, {"action": "disable", "user_id": self.pending.pk})
        self.pending.refresh_from_db()
        self.assertFalse(self.pending.is_active)
        self.client.logout()
        self.assertContains(self.client.post(reverse("login"), {"username": "pending-review", "password": "operator-test-password"}), "账号已停用")
        self.client.force_login(self.admin_user)
        self.client.post(self.url, {"action": "enable", "user_id": self.pending.pk})
        self.pending.refresh_from_db()
        self.assertTrue(self.pending.is_active)

    def test_reject_and_reopen_registration(self):
        self.client.force_login(self.admin_user)
        self.client.post(self.url, {"action": "reject", "user_id": self.pending.pk})
        self.pending.refresh_from_db()
        self.assertFalse(self.pending.is_active)
        self.assertFalse(self.pending.is_staff)
        self.assertContains(self.client.get(self.url), "已拒绝")
        self.client.logout()
        self.assertContains(self.client.post(reverse("login"), {"username": "pending-review", "password": "operator-test-password"}), "注册申请未通过")
        self.client.force_login(self.admin_user)
        self.client.post(self.url, {"action": "reopen", "user_id": self.pending.pk})
        self.pending.refresh_from_db()
        self.assertTrue(self.pending.is_active)
        self.assertFalse(self.pending.is_staff)

    def test_cannot_change_administrator_account(self):
        self.client.force_login(self.admin_user)
        self.client.post(self.url, {"action": "disable", "user_id": self.admin_user.pk})
        self.admin_user.refresh_from_db()
        self.assertTrue(self.admin_user.is_active)
