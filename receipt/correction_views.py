from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.roles import RECEIPT_ROLES, require_role
from analytical.models import Result
from core.models import AuditEvent

from .correction_forms import (
    CollectionCorrectionForm,
    IdentityCorrectionForm,
    MatrixCorrectionForm,
    ObservationsCorrectionForm,
    OrderReferenceForm,
    TestsChangeForm,
)
from .corrections import (
    change_requested_tests,
    correct_order_reference,
    correct_sample,
    correct_sample_matrix,
    correction_blocker,
    order_reference_blocker,
)
from .models import Order, Sample

# section -> (form class, page title, nav label)
SECTIONS = {
    "identity": (IdentityCorrectionForm, "Correct sample details", "Identity, type, location"),
    "collection": (CollectionCorrectionForm, "Correct collection", "Collection type and times"),
    "observations": (
        ObservationsCorrectionForm,
        "Correct receipt observations",
        "Receipt observations",
    ),
    "tests": (TestsChangeForm, "Add or withdraw tests", "Requested tests"),
    "matrix": (MatrixCorrectionForm, "Correct matrix", "Matrix"),
}
NEEDS_SAMPLE = ("tests", "matrix")
CORRECTION_ACTIONS = [
    "sample.identity_corrected",
    "sample.collection_corrected",
    "sample.observations_corrected",
    "sample.tests_changed",
    "sample.matrix_corrected",
]


def _history_value(field, value):
    if field in ("collected_at", "collection_started_at") and value:
        parsed = parse_datetime(value)
        if parsed is not None and timezone.is_aware(parsed):
            return timezone.localtime(parsed).strftime("%Y-%m-%d %H:%M:%S %Z")
    if field in ("collection_type", "sample_type", "source_type", "coolant"):
        return dict(Sample._meta.get_field(field).choices).get(value, value)
    return value


def correction_events(sample):
    events = (
        AuditEvent.objects.filter(
            object_type="receipt.sample",
            object_id=str(sample.pk),
            action__in=CORRECTION_ACTIONS,
        )
        .select_related("actor")
        .order_by("-occurred_at")
    )

    labels = {
        "client_sample_id": "Client sample ID",
        "sample_type": "Sample type",
        "source_type": "Sample source",
        "collector_name": "Collector",
        "collection_location": "Collection location",
        "remarks": "Sample remarks",
        "collection_type": "Collection type",
        "collection_started_at": "Composite start",
        "collected_at": "Collection / composite end",
        "cooler_temperature_original": "Cooler temperature (°C, as read)",
        "temperature_instrument": "Thermometer",
        "coolant": "Coolant on arrival",
        "seal_condition": "Custody seal",
        "preservation_check": "Preservation check",
        "discrepancy": "Discrepancy",
    }
    for event in events:
        event.changes = [
            (
                label,
                _history_value(field, event.before.get(field)),
                _history_value(field, event.after.get(field)),
            )
            for field, label in labels.items()
            if event.before.get(field) != event.after.get(field)
        ]
        if event.action in ("sample.tests_changed", "sample.matrix_corrected"):
            if event.before.get("matrix") != event.after.get("matrix"):
                event.changes.append(
                    ("Matrix", event.before.get("matrix"), event.after.get("matrix"))
                )
            before, after = event.before.get("tests", []), event.after.get("tests", [])
            event.changes += [("Test", t, "withdrawn") for t in before if t not in after]
            event.changes += [("Test", "not requested", t) for t in after if t not in before]
    return events


def _form_errors(form, exc):
    if hasattr(exc, "message_dict"):
        for name, errors in exc.message_dict.items():
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def _apply(section, user, sample_id, cleaned):
    if section == "tests":
        change_requested_tests(actor=user, sample_id=sample_id, **cleaned)
    elif section == "matrix":
        correct_sample_matrix(actor=user, sample_id=sample_id, **cleaned)
    else:
        correct_sample(actor=user, sample_id=sample_id, section=section, **cleaned)


@login_required
def sample_correct(request, pk, section):
    require_role(request.user, *RECEIPT_ROLES)
    if section not in SECTIONS:
        raise Http404
    sample = get_object_or_404(Sample.objects.select_related("order__project__client"), pk=pk)
    form_class = SECTIONS[section][0]
    kwargs = {"sample": sample} if section in NEEDS_SAMPLE else {"instance": sample}
    form = form_class(request.POST if request.method == "POST" else None, **kwargs)
    if request.method == "POST" and form.is_valid():
        try:
            _apply(section, request.user, pk, form.cleaned_data)
        except ValidationError as exc:
            _form_errors(form, exc)
        else:
            messages.success(
                request, "Receipt correction recorded. Reprint affected labels if needed."
            )
            return redirect("receipt:sample-detail", pk=pk)
    # ModelForm validation changes its in-memory instance; keep displayed history current.
    sample.refresh_from_db()
    blockers = {name: correction_blocker(sample, name) for name in SECTIONS}
    # Approved results waiting for a report read these receipt facts when the report is built.
    dependent = (
        Result.objects.filter(requested_test__sample=sample, status="approved").count()
        if section == "observations"
        else 0
    )
    return render(
        request,
        "receipt/sample_correction.html",
        {
            "page_title": SECTIONS[section][1],
            "sample": sample,
            "form": form,
            "section": section,
            "section_tabs": [
                {
                    "label": label,
                    "url": reverse("receipt:sample-correct", args=[sample.pk, name]),
                    "current": name == section,
                    "note": "(locked)" if blockers[name] else "",
                }
                for name, (_, _, label) in SECTIONS.items()
            ],
            "blocker": blockers[section],
            "dependent_results": dependent,
            "correction_events": correction_events(sample),
        },
    )


def order_reference_events(order):
    events = (
        AuditEvent.objects.filter(
            object_type="receipt.order",
            object_id=str(order.pk),
            action="order.reference_corrected",
        )
        .select_related("actor")
        .order_by("-occurred_at")
    )
    for event in events:
        event.changes = [
            (
                "Chain of custody reference",
                event.before.get("coc_reference"),
                event.after.get("coc_reference"),
            )
        ]
    return events


@login_required
def order_correct_reference(request, pk):
    require_role(request.user, *RECEIPT_ROLES)
    order = get_object_or_404(Order.objects.select_related("project__client"), pk=pk)
    form = OrderReferenceForm(request.POST or None, order=order)
    if request.method == "POST" and form.is_valid():
        try:
            correct_order_reference(actor=request.user, order_id=pk, **form.cleaned_data)
        except ValidationError as exc:
            _form_errors(form, exc)
        else:
            messages.success(
                request, "Reference corrected. The earlier reference stays in the history."
            )
            return redirect("receipt:order-detail", pk=pk)
    return render(
        request,
        "receipt/order_correction.html",
        {
            "page_title": "Correct reference",
            "order": order,
            "form": form,
            "blocker": order_reference_blocker(order),
            "correction_events": order_reference_events(order),
        },
    )
