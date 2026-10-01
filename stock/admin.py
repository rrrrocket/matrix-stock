from django.contrib import admin

from .models import ExternalOrder, OrderAlert, OrderLine, StockBalance, StockItem, StockMovement, Warehouse


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    search_fields = ("code", "name")


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    list_display = ("sku", "name")
    search_fields = ("sku", "name")


@admin.register(StockBalance)
class StockBalanceAdmin(admin.ModelAdmin):
    list_display = ("warehouse", "item", "on_hand", "updated_at")
    list_select_related = ("warehouse", "item")
    readonly_fields = ("warehouse", "item", "on_hand", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ExternalOrder)
class ExternalOrderAdmin(admin.ModelAdmin):
    list_display = ("platform", "store_name", "external_order_id", "status", "received_at")
    search_fields = ("external_order_id", "store_name")
    readonly_fields = ("platform", "store_external_id", "store_name", "external_order_id", "status", "ordered_at", "received_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrderLine)
class OrderLineAdmin(admin.ModelAdmin):
    list_display = ("order", "sku", "quantity", "outbound_quantity", "is_skipped")
    search_fields = ("sku",)
    readonly_fields = ("order", "sku", "product_name", "quantity", "outbound_quantity", "is_skipped", "is_present")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "warehouse", "item", "movement_type", "quantity_change", "operator")
    list_select_related = ("warehouse", "item", "operator")
    search_fields = ("item__sku", "order_line__order__external_order_id")
    readonly_fields = ("operation_id", "warehouse", "item", "order_line", "movement_type", "quantity_change", "before_quantity", "after_quantity", "note", "operator", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OrderAlert)
class OrderAlertAdmin(admin.ModelAdmin):
    list_display = ("order", "status", "attempts", "sent_at", "last_error")
    list_select_related = ("order",)
    readonly_fields = ("fingerprint", "attempts", "last_error", "sent_at", "created_at")
