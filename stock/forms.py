from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError


class StockLoginForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise ValidationError("账号正在等待管理员审核，请审核通过后再登录。", code="pending_approval")


class StockRegistrationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        fields = ("username",)

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = False
        if commit:
            user.save()
        return user
