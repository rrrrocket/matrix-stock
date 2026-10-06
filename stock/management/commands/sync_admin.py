import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or update the administrator from STOCK_ADMIN_USERNAME and STOCK_ADMIN_PASSWORD."

    def handle(self, *args, **options):
        username = os.environ.get("STOCK_ADMIN_USERNAME", "").strip()
        password = os.environ.get("STOCK_ADMIN_PASSWORD", "")
        if not username and not password:
            return
        if not username or not password:
            raise CommandError("STOCK_ADMIN_USERNAME 和 STOCK_ADMIN_PASSWORD 必须同时设置")

        user_model = get_user_model()
        user = user_model.objects.filter(username=username).first()
        if user is None:
            user_model.objects.create_superuser(username=username, password=password)
            self.stdout.write(self.style.SUCCESS(f"已创建管理员 {username}"))
            return
        if not user.is_superuser or not user.is_staff:
            raise CommandError(f"账号 {username} 已存在，但不是管理员")
        if not user.check_password(password):
            user.set_password(password)
            user.save(update_fields=["password"])
            self.stdout.write(self.style.SUCCESS(f"已更新管理员 {username} 的密码"))
