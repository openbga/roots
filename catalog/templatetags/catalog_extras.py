from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def plain_decimal(value):
    """Exact decimal text without storage-scale zeros: 0.01000000 -> 0.01, 2.00000000 -> 2."""
    if value is None or value == "":
        return "—"
    try:
        return format(Decimal(value).normalize(), "f")
    except InvalidOperation:
        return str(value)
