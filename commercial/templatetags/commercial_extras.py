from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def usd(value):
    """Exact US-dollar text from a Decimal: 1234.5 -> $1,234.50, -5 -> -$5.00. Never a float."""
    if value is None or value == "":
        return "—"
    try:
        amount = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        return str(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


TONES = {
    "draft": "neutral",
    "issued": "info",
    "revising": "warning",
    "accepted": "success",
    "declined": "danger",
    "withdrawn": "neutral",
    "expired": "warning",
    "submitted": "progress",
    "returned": "warning",
}


@register.filter
def tone(value):
    return TONES.get(value, "neutral")
