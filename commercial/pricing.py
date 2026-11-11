"""Price lists: exact USD prices per catalog test. Quotes copy prices; edits never rewrite them."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from catalog.models import TestDefinition
from core.audit import record_event

from .models import PriceList, PriceListItem
from .permissions import PRICING_ROLES, require_role


def offered_tests():
    """Tests a list may price: active test on an active method."""
    return TestDefinition.objects.filter(is_active=True, method__is_active=True).select_related(
        "method", "analyte"
    )


def test_label(test):
    return (
        f"{test.analyte.name} · {test.method.code} ({test.method.version}) · "
        f"{test.get_matrix_display()} · {test.unit}"
    )


def _prices(price_list):
    return {
        str(item.test_id): str(item.unit_price) for item in price_list.items.order_by("test_id")
    }


@transaction.atomic
def save_price_list(
    *,
    actor,
    price_list_id=None,
    expected_revision=None,
    name,
    description="",
    is_active=True,
    prices,
    reason="",
):
    """Create or edit a list. `prices` maps TestDefinition pk -> Decimal, or None to not offer it.
    Only offered tests can be newly priced; a retired test's price can only be cleared."""
    require_role(actor, *PRICING_ROLES)
    name = (name or "").strip()
    if not name:
        raise ValidationError({"name": "Give the price list a name."})
    for price in prices.values():
        if price is not None and (not isinstance(price, Decimal) or not price.is_finite()):
            raise ValidationError("Prices must be exact decimal amounts.")
        if price is not None and (price < 0 or price != price.quantize(Decimal("0.01"))):
            raise ValidationError("Prices must be non-negative US dollar amounts in whole cents.")
    if price_list_id is None:
        obj = PriceList(name=name, description=description, is_active=is_active)
        before = {}
    else:
        obj = PriceList.objects.select_for_update().get(pk=price_list_id)
        if obj.revision != expected_revision:
            raise ValidationError(
                "This price list changed. Reload it and review the current prices."
            )
        if not (reason or "").strip():
            raise ValidationError({"reason": "Give the reason for this change."})
        before = {
            "name": obj.name,
            "description": obj.description,
            "is_active": obj.is_active,
            "prices": _prices(obj),
        }
        obj.name, obj.description, obj.is_active = name, description, is_active
        obj.revision += 1
    if PriceList.objects.filter(name__iexact=name).exclude(pk=obj.pk).exists():
        raise ValidationError({"name": "A price list with this name already exists."})
    obj.full_clean()
    obj.save()
    existing = {item.test_id: item for item in obj.items.all()}
    allowed = set(offered_tests().values_list("pk", flat=True))
    for test_id, price in prices.items():
        item = existing.get(test_id)
        if price is None:
            if item:
                item.delete()
        elif item:
            if item.unit_price != price:
                item.unit_price = price
                item.save(update_fields=["unit_price"])
        else:
            if test_id not in allowed:
                raise ValidationError("Only active tests on active methods can be newly priced.")
            PriceListItem.objects.create(price_list=obj, test_id=test_id, unit_price=price)
    after = {
        "name": obj.name,
        "description": obj.description,
        "is_active": obj.is_active,
        "prices": _prices(obj),
    }
    record_event(
        actor=actor,
        action="pricelist.updated" if before else "pricelist.created",
        obj=obj,
        before=before,
        after=after,
        reason=(reason or "").strip(),
    )
    return obj
