"""Server-rendered grid lists on the shared core.grids contract (search, filters, sort, paging)."""

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
    refine=None,
    search_extra=None,
    form_kwargs=None,
    extra=None,
    inline=(),
    more=(),
):
    """`refine(queryset, cleaned)` applies the named filters; `search_extra(q)` adds an OR clause.
    Invalid filters render an error state, never all rows. `inline` and `more` name the filter
    fields for the toolbar (up to three in the row, the rest behind More filters)."""
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
        if refine:
            queryset = refine(queryset, cleaned)
    else:
        queryset = queryset.none()
    chips = active_filters(request, form) if form.is_valid() else []
    cleaned = getattr(form, "cleaned_data", {})
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
            "inline": [form[name] for name in inline if name in form.fields],
            "more": [form[name] for name in more if name in form.fields],
            "more_open": any(cleaned.get(name) for name in more),
            **(extra or {}),
        },
    )
