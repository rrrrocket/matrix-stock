from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group


admin.site.site_header = "库存管理系统"
admin.site.site_title = "用户管理"
admin.site.index_title = "用户管理"
admin.site.enable_nav_sidebar = False

# 用户审核由 Stock 管理页处理，不开放 Django 默认模型编辑界面。
admin.site.unregister(get_user_model())
admin.site.unregister(Group)
