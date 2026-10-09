from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import ValidationError


class StockLoginForm(AuthenticationForm):
    def clean(self):
        try:
            return super().clean()
        except ValidationError as error:
            username = self.cleaned_data.get("username")
            password = self.cleaned_data.get("password")
            if username and password:
                try:
                    user = get_user_model()._default_manager.get_by_natural_key(username)
                except get_user_model().DoesNotExist:
                    pass
                else:
                    if not user.is_active and user.check_password(password):
                        message = "账号已停用，请联系管理员。" if user.is_staff else "注册申请未通过，请联系管理员。"
                        raise ValidationError(message, code="inactive_account") from error
            raise

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
