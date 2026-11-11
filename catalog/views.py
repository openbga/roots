"""Catalog pages. Every staff user can read; only the admin role changes anything.

Lists follow the shared grid contract: one URL with `q`, `sort`, `page`, `per_page` and named
filters, rendered by the server.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import CharField, Count, F, OuterRef, Q, Subquery
from django.db.models.functions import Cast
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.roles import require_role, require_staff
from core.concurrency import protect_form
from core.grids import GridRequest, active_filters
from core.models import AuditEvent

from . import services
from .forms import (
    AnalyteFilterForm,
    AnalyteForm,
    DefinitionFilterForm,
    DefinitionForm,
    MethodFilterForm,
    MethodForm,
    NewVersionForm,
)
from .models import Analyte, Method, TestDefinition

TEST_COLUMNS = {
    "analyte": "analyte__name",
    "method": "method__code",
    "matrix": "matrix",
    "unit": "unit",
    "technique": "method__technique",
    "group": "analyte__group",
    "reporting_limit": "reporting_limit",
    "status": "is_active",
    "requested": "requested",
    "changed_at": "changed_at",
}
METHOD_COLUMNS = {
    "method": "code",
    "version": "version",
    "title": "title",
    "technique": "technique",
    "reference": "reference",
    "tests": "test_total",
    "active_tests": "active_tests",
    "used": "used_total",
    "status": "is_active",
    "changed_at": "changed_at",
}
ANALYTE_COLUMNS = {
    "name": "name",
    "cas_number": "cas_number",
    "group": "group",
    "tests": "test_total",
    "active_tests": "active_tests",
    "requested": "requested",
}


def _last_change(model):
    """Time of the newest audit event for each row, for the optional 'Last changed' column."""
    return Subquery(
        AuditEvent.objects.filter(
            object_type=model._meta.label_lower, object_id=Cast(OuterRef("pk"), CharField())
        )
        .order_by("-occurred_at")
        .values("occurred_at")[:1]
    )


def _grid(
    request,
    *,
    queryset,
    columns,
    default_sort,
    form_class,
    narrow,
    template,
    title,
    grid_key,
    inline,
    more,
):
    """Authorize first, filter by a validated form, then page the ordered rows."""
    require_staff(request.user)
    try:
        grid = GridRequest.parse(request.GET, columns, default_sort)
    except ValueError:  # bad per_page: the bound form reports it and the table stays empty
        grid = GridRequest.parse({}, columns, default_sort)
    data = request.GET.copy()
    data["sort"] = grid.sort
    form = form_class(data)
    if form.is_valid():
        queryset = narrow(queryset, form.cleaned_data)
        chips = active_filters(request, form)
    else:
        queryset = queryset.none()  # never silently widen an invalid filter
        chips = []
    cleaned = getattr(form, "cleaned_data", {})
    advanced_open = bool(form.errors) or any(cleaned.get(n) for n in more)
    return render(
        request,
        template,
        {
            "page_title": title,
            "page_obj": grid.page(queryset, columns),
            "filter_form": form,
            "active_filters": chips,
            "sort": grid.sort,
            "per_page": grid.per_page,
            "can_edit": services.can_edit(request.user),
            "filtered": bool(chips),
            "grid_key": grid_key,
            "inline": [form[name] for name in inline],
            "more": [form[name] for name in more],
            "more_open": advanced_open,
        },
    )


def _contains(queryset, query, *fields):
    if not query:
        return queryset
    condition = Q()
    for field in fields:
        condition |= Q(**{f"{field}__icontains": query})
    return queryset.filter(condition)


def _yes_no(queryset, value, field):
    if value == "yes":
        return queryset.filter(**{f"{field}__gt": 0})
    if value == "no":
        return queryset.filter(**{field: 0})
    return queryset


# ------------------------------------------------------------------------------ lists


@login_required
def test_list(request):
    tests = TestDefinition.objects.select_related("method", "analyte").annotate(
        requested=Count("requestedtest"), changed_at=_last_change(TestDefinition)
    )

    def narrow(qs, f):
        qs = _contains(qs, f["q"], "analyte__name", "method__code", "method__title", "unit")
        for name, lookup in (("method", "method"), ("analyte", "analyte")):
            if f[name]:
                qs = qs.filter(**{lookup: f[name]})
        for name, lookup in (("code", "method__code"), ("matrix", "matrix"), ("unit", "unit")):
            if f[name]:
                qs = qs.filter(**{lookup: f[name]})
        if f["status"] == "available":
            qs = qs.filter(is_active=True, method__is_active=True)
        elif f["status"] in {"active", "inactive"}:
            qs = qs.filter(is_active=f["status"] == "active")
        if f["group"]:
            qs = qs.filter(analyte__group=f["group"])
        if f["limit"]:
            qs = qs.filter(reporting_limit__isnull=f["limit"] == "none")
        return _yes_no(qs, f["used"], "requested")

    return _grid(
        request,
        queryset=tests,
        columns=TEST_COLUMNS,
        default_sort="analyte",
        form_class=DefinitionFilterForm,
        narrow=narrow,
        template="catalog/test_list.html",
        grid_key="catalog.tests",
        inline=("method", "matrix", "status"),
        more=("code", "analyte", "group", "unit", "limit", "used"),
        title="Catalog: tests",
    )


@login_required
def method_list(request):
    methods = Method.objects.annotate(
        test_total=Count("testdefinition", distinct=True),
        active_tests=Count(
            "testdefinition", filter=Q(testdefinition__is_active=True), distinct=True
        ),
        batches=Count("analytical_batches", distinct=True),
        requested=Count("testdefinition__requestedtest", distinct=True),
        changed_at=_last_change(Method),
    ).annotate(used_total=F("batches") + F("requested"))

    def narrow(qs, f):
        qs = _contains(qs, f["q"], "code", "version", "title", "source")
        if f["code"]:
            qs = qs.filter(code=f["code"])
        for name in ("reference", "technique"):
            if f[name]:
                qs = qs.filter(**{name: f[name]})
        if f["status"]:
            qs = qs.filter(is_active=f["status"] == "active")
        return _yes_no(qs, f["used"], "used_total")

    return _grid(
        request,
        queryset=methods,
        columns=METHOD_COLUMNS,
        default_sort="method",
        form_class=MethodFilterForm,
        narrow=narrow,
        template="catalog/method_list.html",
        grid_key="catalog.methods",
        inline=("code", "status"),
        more=("reference", "technique", "used"),
        title="Catalog: methods",
    )


@login_required
def analyte_list(request):
    analytes = Analyte.objects.annotate(
        test_total=Count("testdefinition", distinct=True),
        active_tests=Count(
            "testdefinition", filter=Q(testdefinition__is_active=True), distinct=True
        ),
        requested=Count("testdefinition__requestedtest", distinct=True),
    )

    def narrow(qs, f):
        qs = _contains(qs, f["q"], "name", "cas_number")
        if f["group"]:
            qs = qs.filter(group=f["group"])
        if f["tests"]:
            qs = qs.filter(test_total__gt=0) if f["tests"] == "any" else qs.filter(test_total=0)
        return _yes_no(qs, f["used"], "requested")

    return _grid(
        request,
        queryset=analytes,
        columns=ANALYTE_COLUMNS,
        default_sort="name",
        form_class=AnalyteFilterForm,
        narrow=narrow,
        template="catalog/analyte_list.html",
        grid_key="catalog.analytes",
        inline=("group", "tests"),
        more=("used",),
        title="Catalog: analytes",
    )


# ------------------------------------------------------------------------------ edits


def _service_errors(form, exc):
    """Show a service ValidationError on the form, per field where it names one."""
    if hasattr(exc, "error_dict"):
        for name, errors in exc.error_dict.items():
            target = name if name in form.fields else None
            for error in errors:
                form.add_error(target, error)
    else:
        for message in exc.messages:
            form.add_error(None, message)


def _history(model, pk):
    return (
        AuditEvent.objects.filter(object_type=model._meta.label_lower, object_id=str(pk))
        .select_related("actor")
        .order_by("-occurred_at")[:20]
    )


def _edit(request, pk, model, form_class, save, noun, list_name, section):
    require_role(request.user, *services.CATALOG_ROLES)
    instance = get_object_or_404(model, pk=pk) if pk else None
    form = form_class(request.POST or None, instance=instance)
    protect_form(form, instance)
    if request.method == "POST" and form.is_valid():
        try:
            saved, changed = save(
                actor=request.user,
                instance=form.save(commit=False),
                reason=form.cleaned_data.get("reason", ""),
                expected_state=form.cleaned_data.get("original_state"),
            )
        except ValidationError as exc:
            _service_errors(form, exc)
        else:
            if changed:
                messages.success(request, f"{noun.capitalize()} saved.")
            else:
                messages.info(request, "No changes to save.")
            return redirect(list_name)
    thing = "method" if noun == "method version" else noun  # the list's "New method" button
    context = {
        "page_title": f"Edit {instance}" if instance else f"New {thing}",
        "submit_label": "Save changes" if instance else f"Create {thing}",
        "section": section,
        "form": form,
        "instance": instance,
        "cancel_url": list_name,
        "history": _history(model, instance.pk) if instance else [],
        "facts": services.usage_facts(instance) if instance else [],
        "units": form_class is DefinitionForm,
    }
    if instance and model is Method:
        context["related_url"] = reverse("catalog:test-list") + f"?method={instance.pk}"
        context["related_label"] = "View this version's tests"
        context["new_version_url"] = reverse("catalog:method-new-version", args=[instance.pk])
    elif instance and model is Analyte:
        context["related_url"] = reverse("catalog:test-list") + f"?analyte={instance.pk}"
        context["related_label"] = "View tests for this analyte"
    return render(request, "catalog/form.html", context)


@login_required
def test_edit(request, pk=None):
    return _edit(
        request,
        pk,
        TestDefinition,
        DefinitionForm,
        services.save_test_definition,
        "test",
        "catalog:test-list",
        "Tests",
    )


@login_required
def method_edit(request, pk=None):
    return _edit(
        request,
        pk,
        Method,
        MethodForm,
        services.save_method,
        "method version",
        "catalog:method-list",
        "Methods",
    )


@login_required
def analyte_edit(request, pk=None):
    return _edit(
        request,
        pk,
        Analyte,
        AnalyteForm,
        services.save_analyte,
        "analyte",
        "catalog:analyte-list",
        "Analytes",
    )


@login_required
def method_new_version(request, pk):
    require_role(request.user, *services.CATALOG_ROLES)
    previous = get_object_or_404(Method, pk=pk)
    form = NewVersionForm(request.POST or None, previous=previous)
    if request.method == "POST" and form.is_valid():
        try:
            new = services.create_method_version(
                actor=request.user, previous=previous, **form.cleaned_data
            )
        except ValidationError as exc:
            _service_errors(form, exc)
        else:
            messages.success(request, f"Created {new}.")
            return redirect("catalog:method-edit", pk=new.pk)
    return render(
        request,
        "catalog/form.html",
        {
            "page_title": f"New version of {previous}",
            "section": "Methods",
            "form": form,
            "instance": previous,
            "cancel_url": "catalog:method-list",
            "history": [],
            "facts": services.usage_facts(previous),
            "facts_title": f"Based on {previous.title}",
            "submit_label": "Create version",
        },
    )
