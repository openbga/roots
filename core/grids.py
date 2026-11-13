"""Server pagination/sort and filter links for progressively enhanced HTML tables."""

from dataclasses import dataclass

from django import forms
from django.core.paginator import Paginator

PAGE_SIZES = (25, 50, 100)


class PageSizeForm(forms.Form):
    """The rows-per-page control of includes/pagination.html; GridRequest.parse checks the value."""

    per_page = forms.TypedChoiceField(
        label="Rows per page", choices=[(size, str(size)) for size in PAGE_SIZES], coerce=int
    )


@dataclass(frozen=True)
class GridRequest:
    page_number: str
    per_page: int
    sort: str
    query: str

    @classmethod
    def parse(cls, params, columns, default_sort):
        try:
            per_page = int(params.get("per_page", 25))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid page size. Choose 25, 50 or 100.") from exc
        if per_page not in PAGE_SIZES:
            raise ValueError("Invalid page size. Choose 25, 50 or 100.")
        sort = params.get("sort", default_sort)
        if sort.removeprefix("-") not in columns:
            sort = default_sort
        return cls(params.get("page", "1"), per_page, sort, params.get("q", "").strip())

    def page(self, queryset, columns):
        field = columns[self.sort.removeprefix("-")]
        ordered = queryset.order_by(("-" if self.sort.startswith("-") else "") + field, "pk")
        return Paginator(ordered, self.per_page).get_page(self.page_number)


def active_filters(request, form):
    """Build removable links from validated, named filters without a query DSL."""
    chips = []
    for name, value in form.cleaned_data.items():
        if name in {"sort", "page", "per_page"} or value in (None, "", "all", [], ()):
            continue
        field = form.fields[name]
        label = field.label or name.replace("_", " ").capitalize()
        display = value
        if hasattr(field, "choices") and not hasattr(field, "queryset"):
            display = dict(field.choices).get(value, value)
        params = request.GET.copy()
        params.pop(name, None)
        params.pop("page", None)
        for saved in getattr(request, "vr_saved_params", ()):
            params.pop(saved, None)  # added from the user's saved settings; re-added on arrival
        chips.append(
            {"label": label, "value": str(display), "remove_url": "?" + params.urlencode()}
        )
    return chips
