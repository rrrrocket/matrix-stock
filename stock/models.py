import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class Warehouse(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="stock_warehouses")
    code = models.CharField("仓库编码", max_length=40)
    name = models.CharField("仓库名称", max_length=100)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "code"], name="unique_owner_warehouse_code")]
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class StockItem(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="stock_items")
    sku = models.CharField("货号", max_length=150)
    name = models.CharField("商品名称", max_length=255, blank=True)
    reference_cost = models.DecimalField("参考成本单价", max_digits=14, decimal_places=4, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "sku"], name="unique_owner_stock_sku")]
        ordering = ["sku"]

    def __str__(self):
        return self.sku


class ErpLinkGrant(models.Model):
    """Short-lived, single-use code proving a Stock staff login to the ERP."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    code_hash = models.CharField(max_length=64, unique=True)
    state = models.TextField()
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)


class StockBalance(models.Model):
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="balances")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="balances")
    on_hand = models.PositiveIntegerField("实物库存", default=0)
    avg_unit_cost = models.DecimalField("库存成本单价", max_digits=14, decimal_places=4, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["warehouse", "item"], name="unique_warehouse_item_balance")]
        ordering = ["warehouse__name", "item__sku"]


class ExternalOrder(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "待处理"
        CANCELLED = "cancelled", "已取消"
        CLOSED = "closed", "已结束"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="stock_orders")
    platform = models.CharField("平台", max_length=40)
    store_external_id = models.CharField("ERP店铺ID", max_length=80)
    store_name = models.CharField("店铺", max_length=150)
    external_order_id = models.CharField("平台订单号", max_length=150)
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.OPEN)
    snapshot_version = models.PositiveBigIntegerField("ERP快照版本", default=0)
    ordered_at = models.DateTimeField("下单时间", null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "platform", "store_external_id", "external_order_id"], name="unique_owner_external_order")]
        ordering = ["-received_at", "-id"]

    def __str__(self):
        return f"{self.platform} / {self.store_name} / {self.external_order_id}"


class OrderLine(models.Model):
    order = models.ForeignKey(ExternalOrder, on_delete=models.PROTECT, related_name="lines")
    sku = models.CharField("订单货号", max_length=150)
    image_url = models.URLField("商品主图", max_length=1000, blank=True)
    quantity = models.PositiveIntegerField("订单数量", validators=[MinValueValidator(1)])
    outbound_quantity = models.PositiveIntegerField("已出库数量", default=0)
    is_skipped = models.BooleanField("暂不处理", default=False)
    is_present = models.BooleanField("仍在订单中", default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["order", "sku"], name="unique_order_sku")]
        ordering = ["id"]

    @property
    def remaining_quantity(self):
        return max(self.quantity - self.outbound_quantity, 0)


class StockMovement(models.Model):
    class Type(models.TextChoices):
        OPENING = "opening", "期初入库"
        INBOUND = "inbound", "人工入库"
        OUTBOUND = "outbound", "订单出库"
        MANUAL_OUTBOUND = "manual_outbound", "手工出库"
        ADJUST = "adjust", "盘点调整"
        EDIT = "edit", "库存编辑"
        RETURN = "return", "退货入库"

    operation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="movements")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="movements")
    order_line = models.ForeignKey(OrderLine, on_delete=models.PROTECT, null=True, blank=True, related_name="movements")
    tracking_number = models.CharField("国内快递单号", max_length=200, blank=True)
    erp_callback_sent_at = models.DateTimeField(null=True, blank=True)
    erp_callback_attempts = models.PositiveIntegerField(default=0)
    erp_callback_next_attempt_at = models.DateTimeField(null=True, blank=True)
    erp_callback_error = models.CharField(max_length=500, blank=True)
    movement_type = models.CharField("类型", max_length=16, choices=Type.choices)
    quantity_change = models.IntegerField("变动数量")
    before_quantity = models.PositiveIntegerField("变动前")
    after_quantity = models.PositiveIntegerField("变动后")
    note = models.CharField("备注", max_length=500, blank=True)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class OutboundDocument(models.Model):
    class Source(models.TextChoices):
        ORDER = "order", "订单出库"
        MANUAL = "manual", "手工出库"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="outbound_documents")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT)
    source = models.CharField(max_length=10, choices=Source.choices)
    request_id = models.UUIDField(unique=True, default=uuid.uuid4)
    document_no = models.CharField(max_length=32, unique=True, blank=True)
    purpose = models.CharField(max_length=200, blank=True)
    recipient = models.CharField(max_length=200, blank=True)
    note = models.CharField(max_length=500, blank=True)
    total_quantity = models.PositiveIntegerField(default=0)
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class OutboundDocumentLine(models.Model):
    document = models.ForeignKey(OutboundDocument, on_delete=models.PROTECT, related_name="lines")
    movement = models.OneToOneField(StockMovement, on_delete=models.PROTECT, related_name="document_line")
    order_line = models.ForeignKey(OrderLine, on_delete=models.PROTECT, null=True, blank=True)
    sku = models.CharField(max_length=150)
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    tracking_number = models.CharField(max_length=200, blank=True)


class OrderAlert(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待发送"
        SENDING = "sending", "发送中"
        SENT = "sent", "已发送"
        SKIPPED = "skipped", "已跳过"

    order = models.OneToOneField(ExternalOrder, on_delete=models.CASCADE, related_name="stock_alert")
    fingerprint = models.CharField(max_length=500)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
