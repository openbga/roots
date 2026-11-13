"""Remote option lookups for large or growing choice lists (the remote select).

The same pattern as Django admin's autocomplete: a `RemoteSelect` renders only the selected
option(s); static/js/controls.js loads the rest page by page from `lookup` as the user types.
Each registered `Lookup` builds its queryset from the request, so every read is authorized on
the server (AGENTS.md rule 4). The form's ModelChoiceField stays the validator: a lookup only
offers options, and a value the field's own queryset rejects is still rejected on submit.

Opt in per field: `forms.ModelChoiceField(queryset=…, widget=RemoteSelect("active-clients"))`.
Short configuration lists stay inline.
"""

from dataclasses import dataclass, field
from functools import reduce
from operator import or_

from django import forms
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET

from accounts.roles import require_staff

MAX_QUERY = 100  # characters of search text considered


@dataclass(frozen=True)
class Lookup:
    """One remote option list.

    queryset: callable(request) -> QuerySet of the records this user may choose from; it raises
        PermissionDenied for a user who may not read them.
    search_fields: ORM paths matched with icontains; every typed word must match one of them.
    label: callable(obj) -> option text (the model's str by default).
    """

    name: str
    queryset: object
    search_fields: tuple
    ordering: tuple
    label: object = str
    page_size: int = 20
    value: object = field(default=lambda obj: str(obj.pk))


LOOKUPS = {}


def register(lookup):
    if lookup.name in LOOKUPS:
        raise ValueError(f"Lookup {lookup.name!r} is already registered.")
    LOOKUPS[lookup.name] = lookup
    return lookup


def model(label):
    return apps.get_model(label)


def staff(build):
    """Staff-only lookup: client-portal, inactive and role-less users are refused."""

    def queryset(request):
        require_staff(request.user)
        return build(request)

    return queryset


def portal_client(build):
    """Client-portal lookup scoped to the signed-in user's own client; staff are refused."""

    def queryset(request):
        user = request.user
        if not user.is_active or user.client_id is None:
            raise PermissionDenied("Only client-portal users have client records here.")
        return build(request).filter(client_id=user.client_id)

    return queryset


def staff_label(user):
    full_name = user.get_full_name()
    return f"{full_name} ({user.username})" if full_name else user.username


CLIENT_SEARCH = ("name", "account_number", "contact_name", "city")
PROJECT_SEARCH = ("name", "number", "client__name")
EQUIPMENT_SEARCH = ("asset_id", "name", "manufacturer", "model", "serial_number")

register(
    Lookup(
        "clients",
        staff(lambda request: model("receipt.Client").objects.all()),
        CLIENT_SEARCH,
        ("name", "pk"),
    )
)
register(
    Lookup(
        "active-clients",
        staff(lambda request: model("receipt.Client").objects.filter(is_active=True)),
        CLIENT_SEARCH,
        ("name", "pk"),
    )
)
register(
    Lookup(
        "projects",
        staff(lambda request: model("receipt.Project").objects.select_related("client")),
        PROJECT_SEARCH,
        ("client__name", "name", "pk"),
    )
)
register(
    Lookup(
        "active-projects",
        staff(
            lambda request: (
                model("receipt.Project")
                .objects.filter(is_active=True, client__is_active=True)
                .select_related("client")
            )
        ),
        PROJECT_SEARCH,
        ("client__name", "name", "pk"),
    )
)
register(
    Lookup(
        "portal-projects",
        portal_client(lambda request: model("receipt.Project").objects.select_related("client")),
        ("name", "number"),
        ("name", "pk"),
        label=lambda project: project.name,
    )
)
register(
    Lookup(
        "staff",
        staff(lambda request: get_user_model().objects.filter(is_active=True, client__isnull=True)),
        ("username", "first_name", "last_name", "email"),
        ("first_name", "last_name", "username"),
        label=staff_label,
    )
)
register(
    Lookup(
        "equipment",
        staff(lambda request: model("laboratory.Equipment").objects.all()),
        EQUIPMENT_SEARCH,
        ("asset_id",),
    )
)
register(
    Lookup(
        "active-equipment",
        staff(lambda request: model("laboratory.Equipment").objects.exclude(status="retired")),
        EQUIPMENT_SEARCH,
        ("asset_id",),
    )
)
register(
    Lookup(
        "lots",
        staff(lambda request: model("laboratory.MaterialLot").objects.select_related("material")),
        ("lot_number", "material__name", "vendor", "vendor_reference"),
        ("material__name", "lot_number", "pk"),
    )
)


def search(queryset, fields, text):
    for term in text.split():
        queryset = queryset.filter(reduce(or_, (Q(**{f"{f}__icontains": term}) for f in fields)))
    return queryset


@login_required
@require_GET
def lookup(request, name):
    """JSON page of options: {"results": [{"value", "text"}], "more": bool}."""
    entry = LOOKUPS.get(name)
    if entry is None:
        raise Http404("No such lookup.")
    queryset = entry.queryset(request)
    queryset = search(queryset, entry.search_fields, request.GET.get("q", "")[:MAX_QUERY])
    paginator = Paginator(queryset.order_by(*entry.ordering), entry.page_size)
    try:
        page = paginator.page(request.GET.get("page") or 1)
    except EmptyPage:
        return JsonResponse({"results": [], "more": False})
    except ValueError:
        page = paginator.page(1)
    results = [{"value": entry.value(obj), "text": entry.label(obj)} for obj in page]
    return JsonResponse({"results": results, "more": page.has_next()})


class RemoteSelect(forms.Select):
    """A select whose options load from a registered lookup as the user types.

    Renders the empty choice and the selected option(s) only, taken from the form field's own
    queryset, so an edit form shows its current value even when the lookup would no longer
    offer it (an inactive manager, a retired instrument).
    """

    def __init__(self, lookup, attrs=None):
        if lookup not in LOOKUPS:
            raise ValueError(f"Unknown lookup {lookup!r}.")
        super().__init__(attrs)
        self.lookup = lookup

    def build_attrs(self, base_attrs, extra_attrs=None):
        attrs = super().build_attrs(base_attrs, extra_attrs)
        attrs["data-vr-lookup"] = reverse("lookup", args=[self.lookup])
        return attrs

    def optgroups(self, name, value, attrs=None):
        field = self.choices.field
        selected = {str(v) for v in value if str(v) not in field.empty_values}
        options = []
        if not self.allow_multiple_selected and field.empty_label is not None:
            options.append(self.create_option(name, "", field.empty_label, not selected, 0))
        to_field = field.to_field_name or "pk"
        chosen = field.queryset.filter(**{f"{to_field}__in": selected}) if selected else []
        for obj in chosen:
            option_value = field.prepare_value(obj)
            options.append(
                self.create_option(
                    name, option_value, field.label_from_instance(obj), True, len(options)
                )
            )
        return [(None, options, 0)]


class RemoteSelectMultiple(RemoteSelect, forms.SelectMultiple):
    """Multiple choice from a lookup (ModelMultipleChoiceField)."""

    allow_multiple_selected = True
