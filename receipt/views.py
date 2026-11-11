from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from accounts.roles import RECEIPT_ROLES, require_role, require_staff
from analytical.preservation_services import preservation_history
from core.models import AuditEvent

from .correction_views import correction_events, order_reference_events
from .defaults import sample_defaults
from .filters import ClientFilter, OrderFilter, ProjectFilter, SampleFilter
from .forms import (
    ClientEditForm,
    ClientForm,
    ContainerFormSet,
    DispositionForm,
    OrderActionForm,
    OrderDetailsForm,
    OrderForm,
    OrderSampleForm,
    ProjectEditForm,
    ProjectForm,
    ReceiptForm,
    TransferForm,
)
from .lists import (
    CLIENT_COLUMNS,
    ORDER_COLUMNS,
    PROJECT_COLUMNS,
    SAMPLE_COLUMNS,
    list_response,
)
from .models import Client, Order, Project, Sample
from .scheduling import RescheduleForm, schedule_response
from .services import (
    cancel_order,
    change_disposition,
    create_client,
    create_order,
    create_project,
    receive_order_sample,
    receive_sample,
    reinstate_order,
    reschedule_order,
    transfer_sample,
    update_client,
    update_order_details,
    update_project,
)
from .work_status import requested_test_work


@login_required
def sample_list(request):
    require_staff(request.user)
    return list_response(
        request,
        queryset=Sample.objects.select_related("order__project__client"),
        columns=SAMPLE_COLUMNS,
        default_sort="-received_at",
        form_class=SampleFilter,
        search_fields=(
            "lab_id",
            "client_sample_id",
            "order__project__client__name",
            "order__project__name",
            "order__coc_reference",
            "collection_location",
            "current_location",
        ),
        filter_fields={
            "matrix": "matrix",
            "disposition": "disposition",
            "sample_type": "sample_type",
            "source_type": "source_type",
            "client": "order__project__client",
            "project": "order__project",
        },
        date_fields=(("received", "received_at"), ("collected", "collected_at")),
        template="receipt/sample_list.html",
        title="Samples",
        placeholder="Lab ID, client sample ID, client, project or location",
        inline=("matrix", "disposition"),
        more=(
            "client",
            "project",
            "sample_type",
            "source_type",
            "received_from",
            "received_to",
            "collected_from",
            "collected_to",
        ),
        extra={
            "disposition": request.GET.get("disposition", ""),
            "dispositions": Sample.Disposition.choices,
        },
    )


@login_required
def sample_create(request):
    require_role(request.user, *RECEIPT_ROLES)
    form = ReceiptForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            sample = receive_sample(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, f"Received {sample.lab_id}.")
            return redirect("receipt:sample-detail", pk=sample.pk)
    return render(
        request, "receipt/sample_form.html", {"page_title": "Receive sample", "form": form}
    )


def _sample_history(custody, dispositions, corrections, assessments):
    """One chronological history (oldest first) of everything recorded about a sample."""
    items = []
    for event in custody:
        if event.from_location:
            move = f"from {event.from_location} to {event.to_location}"
        else:
            move = f"to {event.to_location}"
        recorded = timezone.localtime(event.recorded_at).strftime("%Y-%m-%d %H:%M %Z")
        items.append(
            {
                "at": event.occurred_at,
                "title": f"Custody, {event.get_kind_display().lower()} {move}",
                "actor": event.actor,
                "by": "Recorded by",
                "reason": event.reason,
                "detail": (
                    f"Relinquished by {event.relinquished_by}; received by "
                    f"{event.received_by}. Recorded {recorded}."
                ),
            }
        )
    for event in dispositions:
        before = (event.before.get("disposition") or "none").capitalize()
        after = (event.after.get("disposition") or "none").capitalize()
        items.append(
            {
                "at": event.occurred_at,
                "title": f"Disposition changed from {before} to {after}",
                "actor": event.actor,
                "by": "Recorded by",
                "reason": event.reason,
            }
        )
    for event in corrections:
        items.append(
            {
                "at": event.occurred_at,
                "title": "Record corrected",
                "actor": event.actor,
                "by": "Recorded by",
                "reason": event.reason,
                "changes": event.changes,
            }
        )
    for decision in assessments:
        items.append(
            {
                "at": decision.recorded_at,
                "title": f"Preservation assessment, {decision.test_label}: "
                f"{decision.get_decision_display()}",
                "actor": decision.actor,
                "by": "Recorded by",
                "reason": decision.reason,
                "detail": ""
                if decision.is_current
                else "Superseded: the receipt record, rule or test changed after this assessment.",
            }
        )
    return sorted(items, key=lambda item: item["at"])


@login_required
def sample_detail(request, pk):
    require_staff(request.user)
    sample = get_object_or_404(
        Sample.objects.select_related("order__project__client", "received_by").prefetch_related(
            "containers"
        ),
        pk=pk,
    )
    test_work = requested_test_work(sample)
    history = preservation_history(sample)
    for rt in test_work:
        # An assessment of this test whose evidence has since changed stays visible, not current.
        rt.stale_assessment = next(
            (
                d
                for d in history
                if d.definition_id == rt.definition_id
                and not d.is_current
                and rt.preservation
                and rt.preservation.latest_decision is None
            ),
            None,
        )
    form = TransferForm(request.POST or None, initial={"expected_revision": sample.revision})
    if request.method == "POST":
        require_role(request.user, *RECEIPT_ROLES)
        if form.is_valid():
            try:
                transfer_sample(actor=request.user, sample_id=sample.pk, **form.cleaned_data)
            except ValidationError as exc:
                _errors(form, exc)
            else:
                messages.success(request, "Custody transfer recorded.")
                return redirect("receipt:sample-detail", pk=pk)
    return render(
        request,
        "receipt/sample_detail.html",
        {
            "page_title": sample.lab_id,
            "sample": sample,
            "transfer_form": form,
            "test_work": test_work,
            "test_equipment": {
                rt.batch.equipment for rt in test_work if rt.batch and rt.batch.equipment_id
            },
            "history": _sample_history(
                sample.custody_events.select_related("actor"),
                AuditEvent.objects.filter(
                    object_type="receipt.sample",
                    object_id=str(sample.pk),
                    action="sample.disposition_changed",
                ).select_related("actor"),
                correction_events(sample),
                history,
            ),
        },
    )


@login_required
def client_list(request):
    require_staff(request.user)
    return list_response(
        request,
        queryset=Client.objects.all(),
        columns=CLIENT_COLUMNS,
        default_sort="name",
        form_class=ClientFilter,
        search_fields=("name", "account_number", "contact_name", "email", "city"),
        filter_fields={"state": "state"},
        template="receipt/client_list.html",
        title="Clients",
        placeholder="Client, account code, contact, email or city",
        inline=("active",),
        more=("state",),
    )


@login_required
def project_list(request):
    require_staff(request.user)
    return list_response(
        request,
        queryset=Project.objects.select_related("client", "project_manager"),
        columns=PROJECT_COLUMNS,
        default_sort="name",
        form_class=ProjectFilter,
        search_fields=("name", "number", "client__name", "program", "site_address"),
        filter_fields={
            "client": "client",
            "regulatory_program": "regulatory_program",
            "project_manager": "project_manager",
        },
        template="receipt/project_list.html",
        title="Projects",
        placeholder="Project, number, client, permit or site",
        inline=("client", "active"),
        more=("regulatory_program", "project_manager"),
    )


def _create(request, form_class, service, thing, list_name, destination):
    require_role(request.user, "project_manager")
    initial = {"client": request.GET["client"]} if request.GET.get("client", "").isdigit() else {}
    form = form_class(
        request.POST or None, initial=initial if "client" in form_class.base_fields else {}
    )
    if request.method == "POST" and form.is_valid():
        obj = service(actor=request.user, **form.cleaned_data)
        messages.success(request, f"Create {thing} completed.")
        return destination(obj)
    list_url = reverse(list_name)
    return render(
        request,
        "receipt/form.html",
        {
            "page_title": f"New {thing}",
            "submit_label": f"Create {thing}",
            "form": form,
            "crumbs": [(f"{thing.capitalize()}s", list_url), (f"New {thing}", None)],
            "cancel_url": list_url,
        },
    )


@login_required
def client_create(request):
    return _create(
        request,
        ClientForm,
        create_client,
        "client",
        "receipt:client-list",
        lambda obj: redirect("receipt:client-detail", pk=obj.pk),
    )


@login_required
def project_create(request):
    return _create(
        request,
        ProjectForm,
        create_project,
        "project",
        "receipt:project-list",
        lambda obj: redirect("receipt:project-list"),
    )


@login_required
def order_list(request):
    require_staff(request.user)
    return list_response(
        request,
        queryset=Order.objects.select_related("project__client").annotate(
            sample_count=Count("samples")
        ),
        columns=ORDER_COLUMNS,
        default_sort="-created_at",
        form_class=OrderFilter,
        search_fields=(
            "coc_reference",
            "po_number",
            "carrier_tracking",
            "project__name",
            "project__client__name",
        ),
        filter_fields={
            "client": "project__client",
            "project": "project",
            "priority": "priority",
        },
        date_fields=(("created", "created_at"),),
        template="receipt/order_list.html",
        title="Chains of custody",
        placeholder="Reference, PO, tracking number, client or project",
        inline=("client", "priority"),
        more=("project", "created_from", "created_to"),
    )


@login_required
def order_create(request):
    require_role(request.user, *RECEIPT_ROLES)
    form = OrderForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            order = create_order(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            return redirect("receipt:order-detail", pk=order.pk)
    return render(
        request, "receipt/order_form.html", {"page_title": "New chain of custody", "form": form}
    )


@login_required
def order_detail(request, pk):
    require_staff(request.user)
    order = get_object_or_404(Order.objects.select_related("project__client", "created_by"), pk=pk)
    return render(
        request,
        "receipt/order_detail.html",
        {
            "page_title": order.coc_reference,
            "order": order,
            "samples": order.samples.prefetch_related("containers").order_by("lab_id"),
            "has_submission": hasattr(order, "submission"),
            "action_form": OrderActionForm(initial={"expected_revision": order.revision}),
            "correction_events": order_reference_events(order),
            "detail_events": AuditEvent.objects.filter(
                object_type="receipt.order",
                object_id=str(order.pk),
                action="order.details_updated",
            )
            .select_related("actor")
            .order_by("occurred_at"),
        },
    )


@login_required
def order_edit(request, pk):
    require_role(request.user, *RECEIPT_ROLES)
    order = get_object_or_404(Order.objects.select_related("project__client"), pk=pk)
    if order.is_cancelled:
        messages.error(request, "This chain of custody was cancelled. Reinstate it first.")
        return redirect("receipt:order-detail", pk=pk)
    reference = order.coc_reference  # failed ModelForm validation rewrites the instance
    form = OrderDetailsForm(request.POST or None, instance=order)
    if request.method == "POST" and form.is_valid():
        try:
            update_order_details(actor=request.user, order_id=pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Chain of custody details saved with their reason.")
            return redirect("receipt:order-detail", pk=pk)
    return render(
        request,
        "receipt/order_edit.html",
        {
            "page_title": "Edit chain of custody details",
            "order": order,
            "reference": reference,
            "form": form,
        },
    )


def _order_action(request, pk, service, success):
    require_role(request.user, *RECEIPT_ROLES)
    get_object_or_404(Order, pk=pk)
    form = OrderActionForm(request.POST)
    if form.is_valid():
        try:
            service(actor=request.user, order_id=pk, **form.cleaned_data)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, success)
    else:
        for errors in form.errors.values():
            messages.error(request, " ".join(errors))
    return redirect("receipt:order-detail", pk=pk)


@require_POST
@login_required
def order_cancel(request, pk):
    return _order_action(
        request, pk, cancel_order, "Chain of custody cancelled. The reason stays on record."
    )


@require_POST
@login_required
def order_reinstate(request, pk):
    return _order_action(request, pk, reinstate_order, "Chain of custody reinstated.")


def _defaults_source(request, order):
    """The sample on THIS order named by ?defaults_from=, or None (with a warning) if unusable."""
    raw = request.GET.get("defaults_from")
    if not raw:
        return None
    try:
        source = Sample.objects.filter(pk=UUID(raw), order=order).first()
    except ValueError:
        source = None
    if source is None:
        messages.warning(
            request,
            "Defaults can only come from a sample already received on this chain of custody. "
            "Starting with a blank form.",
        )
    return source


def _next_defaults_url(order_pk, sample_pk):
    query = urlencode({"defaults_from": sample_pk})
    return f"{reverse('receipt:order-add-sample', kwargs={'pk': order_pk})}?{query}"


@login_required
def order_add_sample(request, pk):
    require_role(request.user, *RECEIPT_ROLES)
    order = get_object_or_404(Order.objects.select_related("project__client"), pk=pk)
    if order.is_cancelled:
        messages.error(request, "This chain of custody was cancelled. Reinstate it before receipt.")
        return redirect("receipt:order-detail", pk=pk)
    source = _defaults_source(request, order)
    defaults = sample_defaults(source) if source else None
    initial = {"expected_revision": order.revision, **(defaults["initial"] if defaults else {})}
    form = OrderSampleForm(request.POST or None, initial=initial)
    containers = ContainerFormSet(
        request.POST or None,
        initial=None if request.POST or not defaults else defaults["containers"],
        prefix="containers",
    )
    if defaults and request.method == "GET":
        form.fields["client_sample_id"].widget.attrs["autofocus"] = True
    if request.method == "POST":
        form_valid = form.is_valid()
        containers_valid = containers.is_valid()
        if form_valid and containers_valid:
            rows = [
                {k: v for k, v in data.items() if k != "DELETE"}
                for data in containers.cleaned_data
                if data and not data.get("DELETE")
            ]
            try:
                sample = receive_order_sample(
                    actor=request.user, order_id=pk, containers=rows, **form.cleaned_data
                )
            except ValidationError as exc:
                _errors(form, exc)
            else:
                url = _next_defaults_url(pk, sample.pk)
                if request.POST.get("next") == "defaults":
                    messages.success(
                        request,
                        f"Received {sample.lab_id}. Defaults reused from it are shown below; "
                        "enter this sample's own details.",
                    )
                    return redirect(url)
                messages.success(
                    request,
                    format_html(
                        "Received {}. Continue with the next sample, or "
                        '<a href="{}">receive another using these defaults</a>.',
                        sample.lab_id,
                        url,
                    ),
                )
                return redirect("receipt:order-detail", pk=pk)
    return render(
        request,
        "receipt/order_sample_form.html",
        {
            "page_title": "Receive another sample" if defaults else "Receive sample",
            "order": order,
            "form": form,
            "container_formset": containers,
            "defaults": defaults,
            "offer_next_defaults": True,
        },
    )


def _errors(form, error):
    if hasattr(error, "message_dict"):
        for name, errors in error.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, error)


@login_required
def sample_disposition(request, pk):
    require_role(request.user, *RECEIPT_ROLES)
    sample = get_object_or_404(Sample, pk=pk)
    form = DispositionForm(
        request.POST or None,
        initial={"expected_revision": sample.revision, "disposition": sample.disposition},
    )
    if request.method == "POST" and form.is_valid():
        try:
            change_disposition(actor=request.user, sample_id=pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Disposition updated; prior decision retained in history.")
            return redirect("receipt:sample-detail", pk=pk)
    return render(
        request,
        "receipt/disposition_form.html",
        {"page_title": "Change disposition", "sample": sample, "form": form},
    )


def _edit(request, obj, form_class, service, id_field, thing, list_name, record_name=None):
    """Edit form for a client or project. A record with a page returns to it; otherwise the list."""
    require_role(request.user, "project_manager")
    name = obj.name  # a failed ModelForm validation overwrites the instance with the input
    list_url = reverse(list_name)
    record_url = reverse(record_name, kwargs={"pk": obj.pk}) if record_name else None
    form = form_class(
        request.POST or None, instance=obj, initial={"expected_revision": obj.revision}
    )
    if request.method == "POST" and form.is_valid():
        try:
            service(actor=request.user, **{id_field: obj.pk}, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Changes recorded with their reason.")
            return redirect(record_url or list_url)
    return render(
        request,
        "receipt/form.html",
        {
            "page_title": f"Edit {name}",
            "submit_label": "Save changes",
            "form": form,
            "object": obj,
            "crumbs": [
                (f"{thing.capitalize()}s", list_url),
                (name, record_url),
                ("Edit", None),
            ],
            "cancel_url": record_url or list_url,
        },
    )


@login_required
def client_edit(request, pk):
    require_role(request.user, "project_manager")
    return _edit(
        request,
        get_object_or_404(Client, pk=pk),
        ClientEditForm,
        update_client,
        "client_id",
        "client",
        "receipt:client-list",
        "receipt:client-detail",
    )


@login_required
def project_edit(request, pk):
    require_role(request.user, "project_manager")
    return _edit(
        request,
        get_object_or_404(Project, pk=pk),
        ProjectEditForm,
        update_project,
        "project_id",
        "project",
        "receipt:project-list",
    )


@login_required
def schedule(request):
    require_staff(request.user)
    return schedule_response(request)


def _target_text(parsed, raw):
    if parsed and timezone.is_aware(parsed):
        return timezone.localtime(parsed).strftime("%Y-%m-%d %H:%M %Z")
    return raw or "not set"


@login_required
def order_schedule(request, pk):
    require_role(request.user, *RECEIPT_ROLES)
    order = get_object_or_404(Order.objects.select_related("project__client"), pk=pk)
    form = RescheduleForm(
        request.POST or None,
        initial={
            "expected_revision": order.revision,
            "due_at": order.due_at,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            reschedule_order(actor=request.user, order_id=pk, **form.cleaned_data)
        except ValidationError as exc:
            _errors(form, exc)
        else:
            messages.success(request, "Delivery target updated; prior target retained in history.")
            return redirect("receipt:order-detail", pk=pk)
    events = list(
        AuditEvent.objects.filter(
            object_type="receipt.order",
            object_id=str(pk),
            action="order.rescheduled",
        )
        .select_related("actor")
        .order_by("-occurred_at")
    )
    for event in events:
        event.before_due_at = (
            parse_datetime(event.before["due_at"]) if event.before.get("due_at") else None
        )
        event.after_due_at = (
            parse_datetime(event.after["due_at"]) if event.after.get("due_at") else None
        )
        event.target_change = [
            (
                "Delivery target",
                _target_text(event.before_due_at, event.before.get("due_at")),
                _target_text(event.after_due_at, event.after.get("due_at")),
            )
        ]
    return render(
        request,
        "receipt/order_schedule.html",
        {
            "page_title": "Change delivery target",
            "order": order,
            "form": form,
            "schedule_events": events,
        },
    )


def _label_response(request, samples, filename):
    from django.http import HttpResponse, HttpResponseBadRequest

    from .labels import render_label_pdf, sample_labels

    try:
        copies = int(request.GET.get("copies", "1"))
        labels = sample_labels(samples, kind=request.GET.get("kind", "containers"), copies=copies)
        pdf = render_label_pdf(labels)
    except (ValueError, ValidationError) as exc:
        message = (
            "; ".join(exc.messages) if isinstance(exc, ValidationError) else "Invalid copy count."
        )
        return HttpResponseBadRequest(message)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}.pdf"'
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def order_labels(request, pk):
    require_staff(request.user)
    order = get_object_or_404(Order, pk=pk)
    return _label_response(
        request,
        order.samples.prefetch_related("containers").order_by("lab_id"),
        f"order-{order.pk}-labels",
    )


@login_required
def sample_label_download(request, pk):
    require_staff(request.user)
    sample = get_object_or_404(Sample.objects.prefetch_related("containers"), pk=pk)
    return _label_response(request, [sample], f"sample-{sample.pk}-labels")
