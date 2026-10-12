from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from stock import admin_views, views
from stock.forms import StockLoginForm


urlpatterns = [
    path("admin/", admin.site.admin_view(admin_views.users), name="stock_admin_users"),
    path("admin/", admin.site.urls),
    path("login/", LoginView.as_view(template_name="stock/login.html", authentication_form=StockLoginForm), name="login"),
    path("register/", views.register, name="register"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("warehouses/", views.warehouses, name="warehouses"),
    path("warehouses/<int:warehouse_id>/delete/", views.delete_warehouse, name="delete_warehouse"),
    path("inventory/", views.inventory, name="inventory"),
    path("orders/", views.orders, name="orders"),
    path("movements/", views.movements, name="movements"),
    path("inventory/inbound/", views.inbound, name="inbound"),
    path("inventory/inbound/batch/", views.batch_inbound, name="batch_inbound"),
    path("inventory/<int:balance_id>/quantity/", views.edit_inventory_quantity, name="edit_inventory_quantity"),
    path("inventory/<int:balance_id>/cost/", views.update_inventory_cost, name="update_inventory_cost"),
    path("orders/<int:line_id>/outbound/", views.outbound, name="outbound"),
    path("orders/outbound/batch/", views.batch_order_outbound, name="batch_order_outbound"),
    path("outbound/new/", views.manual_outbound, name="manual_outbound"),
    path("outbound/search/", views.outbound_stock_search, name="outbound_stock_search"),
    path("outbound/documents/", views.outbound_documents, name="outbound_documents"),
    path("outbound/documents/<int:document_id>/", views.outbound_document, name="outbound_document"),
    path("orders/<int:line_id>/skip/", views.skip_order_line, name="skip_order_line"),
    path("orders/<int:line_id>/resume/", views.resume_order_line, name="resume_order_line"),
    path("api/health/", views.health, name="health"),
    path("erp/connect/", views.erp_connect, name="erp_connect"),
    path("api/account/exchange/", views.erp_link_exchange, name="erp_link_exchange"),
    path("api/catalog/", views.catalog_api, name="catalog_api"),
    path("api/orders/", views.order_webhook, name="order_webhook"),
]
