"""Receipt list queries for server-rendered HTML tables."""

from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import render

from core.grids import GridRequest, active_filters

SAMPLE_COLUMNS = {
    "lab_id": "lab_id",
    "client_sample_id": "client_sample_id",
    "client": "order__project__client__name",
    "project": "order__project__name",
    "matrix": "matrix",
    "sample_type": "sample_type",
    "collected_at": "collected_at",
    "received_at": "received_at",
    "disposition": "disposition",
    "current_location": "current_location",
    "collection_location": "collection_location",
    "coc_reference": "order__coc_reference",
}
ORDER_COLUMNS = {
    "due_at": "due_at",
    "coc_reference": "coc_reference",
    "client": "project__client__name",
    "project": "project__name",
    "sample_count": "sample_count",
    "created_at": "created_at",
    "priority": "priority",
    "po_number": "po_number",
}
CLIENT_COLUMNS = {
    name: name
    for name in ("name", "account_number", "contact_name", "email", "city", "state", "is_active")
}
PROJECT_COLUMNS = {
    "name": "name",
    "number": "number",
    "client": "client__name",
    "regulatory_program": "regulatory_program",
    "program": "program",
    "project_manager": "project_manager__first_name",
    "turnaround_days": "turnaround_days",
    "is_active": "is_active",
}


def toolbar_context(form, chips, *, inline=(), more=(), placeholder=""):
    """Context for includes/grid_toolbar.html: fields by role, whether More filters is open."""
    data = form.data if form.is_bound else {}
    return {
        "inline": [form[name] for name in inline],
        "more": [form[name] for name in more],
        "more_open": bool(form.errors) or any(data.get(name) for name in more),
        "active": bool(chips) or bool(form.errors),
        "placeholder": placeholder,
    }


def list_response(
    request,
    *,
    queryset,
    columns,
    default_sort,
    form_class,
    search_fields,
    filter_fields,
    date_fields=(),
    template,
    title,
    inline=(),
    more=(),
    placeholder="",
    extra=None,
):
    try:
        grid = GridRequest.parse(request.GET, columns, default_sort)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data = request.GET.copy()
    data["q"] = grid.query
    form = form_class(data)
    if form.is_valid():
        cleaned = form.cleaned_data
        if cleaned["q"]:
            condition = Q()
            for field in search_fields:
                condition |= Q(**{f"{field}__icontains": cleaned["q"]})
            queryset = queryset.filter(condition)
        for param, field in filter_fields.items():
            if cleaned.get(param):
                queryset = queryset.filter(**{field: cleaned[param]})
        if cleaned.get("active") in {"yes", "no"}:
            queryset = queryset.filter(is_active=cleaned["active"] == "yes")
        for prefix, field in date_fields:
            for suffix, lookup in (("from", "gte"), ("to", "lte")):
                if value := cleaned.get(f"{prefix}_{suffix}"):
                    queryset = queryset.filter(**{f"{field}__date__{lookup}": value})
    else:
        queryset = queryset.none()
    chips = active_filters(request, form)
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
            **toolbar_context(form, chips, inline=inline, more=more, placeholder=placeholder),
            **(extra or {}),
        },
    )
