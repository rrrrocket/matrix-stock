from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods


def _search(queryset, term):
    if term:
        return queryset.filter(Q(username__icontains=term) | Q(email__icontains=term))
    return queryset


def _update_access(request):
    action = request.POST.get("action", "")
    user_id = request.POST.get("user_id", "")
    if action not in {"approve", "reject", "disable", "enable", "reopen"} or not user_id.isdecimal() or len(user_id) > 18:
        return HttpResponseBadRequest("无效的用户操作")

    users = get_user_model().objects.filter(pk=int(user_id), is_superuser=False).exclude(pk=request.user.pk)
    if action == "approve":
        count = users.filter(is_staff=False, is_active=True).update(is_staff=True)
        success = "注册申请已通过"
    elif action == "reject":
        count = users.filter(is_staff=False, is_active=True).update(is_active=False)
        success = "注册申请已拒绝"
    elif action == "disable":
        count = users.filter(is_staff=True, is_active=True).update(is_active=False)
        success = "用户已停用"
    elif action == "enable":
        count = users.filter(is_staff=True, is_active=False).update(is_active=True)
        success = "用户已启用"
    else:
        count = users.filter(is_staff=False, is_active=False).update(is_active=True)
        success = "已恢复为待审核"

    if count:
        messages.success(request, success)
    else:
        messages.error(request, "账号状态已变化，请刷新后重试")
    section = "pending" if action in {"approve", "reject", "reopen"} else "users"
    return redirect(f"{reverse('stock_admin_users')}#{section}")


@require_http_methods(["GET", "POST"])
def users(request):
    if not request.user.has_perm("auth.change_user"):
        raise PermissionDenied("只有用户管理员可以访问")
    if request.method == "POST":
        return _update_access(request)

    user_model = get_user_model()
    pending_search = request.GET.get("pending_q", "").strip()[:100]
    users_search = request.GET.get("users_q", "").strip()[:100]
    pending = _search(user_model.objects.filter(is_staff=False, is_active=True), pending_search)
    all_users = _search(user_model.objects.exclude(pk=request.user.pk), users_search)
    pending_page = Paginator(pending.order_by("-date_joined", "-pk"), 20).get_page(request.GET.get("pending_page"))
    users_page = Paginator(all_users.order_by("-date_joined", "-pk"), 20).get_page(request.GET.get("users_page"))
    return render(request, "stock/admin_users.html", {
        "pending_page": pending_page,
        "users_page": users_page,
        "pending_search": pending_search,
        "users_search": users_search,
        "pending_total": user_model.objects.filter(is_staff=False, is_active=True).count(),
    })
