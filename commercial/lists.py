"""Server-rendered grid lists on the shared core.grids contract."""

from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import render

from core.grids import GridRequest, active_filters


def grid_response(
    request,
    *,
    queryset,
    columns,
    default_sort,
    form_class,
    search_fields,
    template,
    title,
    filter_fields=None,
    refine=None,
    search_extra=None,
    form_kwargs=None,
    extra=None,
):
    """`search_extra(q)` returns an extra Q to OR into the search. `filter_fields` maps a form
    field to an ORM lookup; `refine(queryset, cleaned)` may handle filters that need more than one
    lookup. Invalid filters show an error, never all rows."""
    try:
        grid = GridRequest.parse(request.GET, columns, default_sort)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data = request.GET.copy()
    data["q"] = grid.query
    form = form_class(data, **(form_kwargs or {}))
    if form.is_valid():
        cleaned = form.cleaned_data
        if cleaned.get("q"):
            condition = Q()
            for field in search_fields:
                condition |= Q(**{f"{field}__icontains": cleaned["q"]})
            if search_extra:
                condition |= search_extra(cleaned["q"])
            queryset = queryset.filter(condition)
        for param, lookup in (filter_fields or {}).items():
            if cleaned.get(param):
                queryset = queryset.filter(**{lookup: cleaned[param]})
        if refine:
            queryset = refine(queryset, cleaned)
    else:
        queryset = queryset.none()
    chips = active_filters(request, form) if form.is_valid() else []
    return render(
        request,
        template,
        {
            "page_title": title,
            "page_obj": grid.page(queryset, columns),
            "filter_form": form,
            "q": grid.query,
            "sort": grid.sort,
            "per_page": grid.per_page,
            "active_filters": chips,
            "filtered": bool(chips or grid.query),
            "inline": [field for field in form.visible_fields() if field.name != "q"],
            **(extra or {}),
        },
    )
