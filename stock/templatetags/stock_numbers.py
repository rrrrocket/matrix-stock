from decimal import Decimal

from django import template


register = template.Library()


@register.filter
def compact_decimal(value):
    if value is None or value == "":
        return ""
    return format(Decimal(value).normalize(), "f")
