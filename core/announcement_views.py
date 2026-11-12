"""Announcement pages: the admin role's list, the post/change form and the withdraw action.

The list follows the shared grid contract (`q`, `sort`, `page`, `per_page` and named filters).
What readers are shown is decided in core.announcements and rendered by the `announcements` tag.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.roles import require_role

from . import announcements as service
from .announcement_forms import AnnouncementFilterForm, AnnouncementForm
from .concurrency import protect_form
from .grids import GridRequest, active_filters
from .models import Announcement, AuditEvent

COLUMNS = {
    "title": "title",
    "status": "state",
    "tone": "tone",
    "starts_at": "starts_at",
    "ends_at": "ends_at",
}
DEFAULT_SORT = "-starts_at"


def _rows(page_obj):
    """Display values for the page's rows: status label and tone, and where each is shown."""
    for row in page_obj:
        row.state_label, row.state_tone = service.STATES[row.state]
        row.audience_labels = service.audience_labels(row)
    return page_obj


@login_required
def announcement_list(request):
    require_role(request.user, *service.ANNOUNCEMENT_ROLES)
    try:
        grid = GridRequest.parse(request.GET, COLUMNS, DEFAULT_SORT)
    except ValueError:  # bad per_page: the bound form reports it and the table stays empty
        grid = GridRequest.parse({}, COLUMNS, DEFAULT_SORT)
    data = request.GET.copy()
    data["sort"] = grid.sort
    form = AnnouncementFilterForm(data)
    queryset = service.with_state(Announcement.objects.all(), timezone.now())
    if form.is_valid():
        filters = form.cleaned_data
        if filters["q"]:
            queryset = queryset.filter(
                Q(title__icontains=filters["q"]) | Q(body__icontains=filters["q"])
            )
        if filters["state"]:
            queryset = queryset.filter(state=filters["state"])
        if filters["audience"]:
            queryset = queryset.filter(**{service.AUDIENCES[filters["audience"]][0]: True})
        chips = active_filters(request, form)
    else:
        queryset = queryset.none()  # never silently widen an invalid filter
        chips = []
    return render(
        request,
        "core/announcement_list.html",
        {
            "page_title": "Announcements",
            "page_obj": _rows(grid.page(queryset, COLUMNS)),
            "filter_form": form,
            "active_filters": chips,
            "sort": grid.sort,
            "filtered": bool(chips),
            "inline": [form["state"], form["audience"]],
        },
    )


def _service_errors(form, exc):
    """Show a service ValidationError on the form, under the field it names where it names one."""
    if hasattr(exc, "error_dict"):
        for name, errors in exc.error_dict.items():
            for error in errors:
                form.add_error(name if name in form.fields else None, error)
    else:
        for message in exc.messages:
            form.add_error(None, message)


@login_required
def announcement_edit(request, pk=None):
    require_role(request.user, *service.ANNOUNCEMENT_ROLES)
    instance = get_object_or_404(Announcement, pk=pk) if pk else None
    withdrawn = bool(instance and instance.withdrawn_at)
    form = AnnouncementForm(request.POST or None, instance=instance)
    protect_form(form, instance)
    if withdrawn:
        for field in form.fields.values():
            field.disabled = True
    if request.method == "POST" and form.is_valid():
        try:
            _, changed = service.save_announcement(
                actor=request.user,
                instance=form.save(commit=False),
                expected_state=form.cleaned_data.get("original_state"),
            )
        except ValidationError as exc:
            _service_errors(form, exc)
        else:
            if not changed:
                messages.info(request, "No changes to save.")
            elif instance:
                messages.success(request, "Announcement saved.")
            else:
                messages.success(request, "Announcement posted.")
            return redirect("announcement-list")
    history = []
    if instance:
        history = (
            AuditEvent.objects.filter(
                object_type=Announcement._meta.label_lower, object_id=str(instance.pk)
            )
            .select_related("actor")
            .order_by("-occurred_at")[:20]
        )
    return render(
        request,
        "core/announcement_form.html",
        {
            "page_title": "New announcement"
            if not instance
            else (instance.title if withdrawn else f"Edit {instance.title}"),
            "submit_label": "Save changes" if instance else "Post announcement",
            "form": form,
            "instance": instance,
            "withdrawn": withdrawn,
            "history": history,
        },
    )


@login_required
@require_POST
def announcement_withdraw(request, pk):
    require_role(request.user, *service.ANNOUNCEMENT_ROLES)
    try:
        row = service.withdraw_announcement(actor=request.user, pk=pk)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"Withdrew “{row.title}”. It is no longer shown anywhere.")
    return redirect("announcement-list")
