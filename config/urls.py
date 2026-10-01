from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from stock import views


urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", LoginView.as_view(template_name="stock/login.html"), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("warehouses/", views.warehouses, name="warehouses"),
    path("inventory/", views.inventory, name="inventory"),
    path("orders/", views.orders, name="orders"),
    path("movements/", views.movements, name="movements"),
    path("inventory/inbound/", views.inbound, name="inbound"),
    path("inventory/import/", views.import_opening, name="import_opening"),
    path("inventory/adjust/", views.adjust, name="adjust"),
    path("orders/<int:line_id>/outbound/", views.outbound, name="outbound"),
    path("orders/<int:line_id>/skip/", views.skip_order_line, name="skip_order_line"),
    path("api/health/", views.health, name="health"),
    path("api/orders/", views.order_webhook, name="order_webhook"),
]
