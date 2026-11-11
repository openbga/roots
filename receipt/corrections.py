"""Bounded receipt corrections; frozen analytical inputs and issued records stay intact."""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.utils import timezone

from accounts.roles import RECEIPT_ROLES, require_role
from analytical.models import QCSample, Result
from analytical.preparation import preparation_dependencies
from core.audit import record_event, snapshot
from reporting.models import ReportVersion

from .models import Order, RequestedTest, Sample, SampleStorage, Subcontract
from .widgets import test_option_data

SECTIONS = {
    "identity": (
        "client_sample_id",
        "sample_type",
        "source_type",
        "collector_name",
        "collection_location",
        "remarks",
    ),
    "collection": ("collection_type", "collection_started_at", "collected_at"),
    "observations": (
        "cooler_temperature",
        "cooler_temperature_original",
        "temperature_instrument",
        "coolant",
        "seal_condition",
        "preservation_check",
        "discrepancy",
    ),
}
# Identity details that may be left blank.
OPTIONAL_IDENTITY = ("collection_location", "source_type", "remarks")
# Sections handled by their own service (a different shape than editing sample columns).
OTHER_SECTIONS = ("matrix", "tests")
ISSUED_MESSAGE = (
    "An issued report contains this sample. This receipt correction path cannot change "
    "released sample metadata; controlled post-release correction is not supported here. "
    "The original report and receipt remain available."
)


def _issued(lab_ids):
    """True when an issued, immutable report snapshot lists any of these samples.

    Report.samples is editable draft scope, so the stored snapshot is the authority."""
    ids = list(lab_ids)
    if not ids:
        return False
    condition = Q()
    for lab_id in ids:
        condition |= Q(file__snapshot__content__samples__contains=[{"lab_id": lab_id}])
    return ReportVersion.objects.filter(condition).exists()


def analytical_work(sample):
    """What already depends on this sample's matrix and collection facts."""
    reasons = []
    if Result.objects.filter(requested_test__sample=sample).exists():
        reasons.append("an internal analytical work record")
    if QCSample.objects.filter(parent_sample=sample).exists():
        reasons.append("a laboratory QC sample")
    if Subcontract.objects.filter(requested_test__sample=sample).exists():
        reasons.append("an external dispatch record")
    if prepared := preparation_dependencies(sample):
        # Aliquot membership is analytical work even before any result exists, open or closed.
        reasons.append(
            "recorded preparation work ("
            + ", ".join(sorted({f"{d['aliquot_label']} in {d['batch_number']}" for d in prepared}))
            + ")"
        )
    return reasons


def correction_blocker(sample, section):
    if SampleStorage.objects.filter(sample=sample, disposed_at__isnull=False).exists():
        return "This sample was physically disposed. Receipt corrections are not available."
    # Added tests are new work and rewrite nothing issued; every other section changes
    # metadata an issued report may already have frozen.
    if section != "tests" and _issued([sample.lab_id]):
        return ISSUED_MESSAGE
    if section in ("collection", "matrix"):
        if reasons := analytical_work(sample):
            if section == "matrix":
                return (
                    "The matrix cannot change because this sample has "
                    + ", ".join(reasons)
                    + ". That work was planned and measured under the recorded matrix. "
                    "Requested tests can still be added while the sample is not rejected."
                )
            return (
                "Collection type and times cannot change because this sample has "
                + ", ".join(reasons)
                + ". Frozen holding-time inputs and source history must stay intact. "
                "Client sample ID, collector and location remain correctable before report issue."
            )
    return ""


def _lock_sample(sample_id, expected_revision):
    # Assignment and dispatch take these locks before creating their dependent records.
    # Do not acquire Result/Batch locks after Sample: report release uses the reverse order.
    list(
        RequestedTest.objects.select_for_update()
        .filter(sample_id=sample_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    sample = Sample.objects.select_for_update().get(pk=sample_id)
    if sample.revision != expected_revision:
        raise ValidationError("Sample changed. Reload and review its current receipt history.")
    return sample


def _reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("A correction reason is required.")
    if len(reason) > 4000:
        raise ValidationError("Correction reason must fit 4000 characters.")
    return reason.strip()


@transaction.atomic
def correct_sample(*, actor, sample_id, expected_revision, section, reason, **changes):
    require_role(actor, *RECEIPT_ROLES)
    if section not in SECTIONS or set(changes) - set(SECTIONS[section]):
        raise ValidationError(
            "Only the selected identity, collection or receipt observation fields can be "
            "corrected. Accession, order and custody events cannot change through this path."
        )
    reason = _reason(reason)
    sample = _lock_sample(sample_id, expected_revision)
    if blocker := correction_blocker(sample, section):
        raise ValidationError(blocker)
    before = {key: value for key, value in snapshot(sample).items() if key in SECTIONS[section]}
    before["revision"] = sample.revision
    for name, value in changes.items():
        if section == "identity":
            if not isinstance(value, str) or (name not in OPTIONAL_IDENTITY and not value.strip()):
                raise ValidationError({name: "This receipt detail is required."})
            value = value.strip()
        setattr(sample, name, value)
    if section == "identity" and not Sample.source_matches(sample.matrix, sample.source_type):
        raise ValidationError(
            {"source_type": "Choose a sample source that belongs to the sample matrix."}
        )
    if section == "collection":
        for name in ("collected_at", "collection_started_at"):
            value = getattr(sample, name)
            if value is None and name == "collection_started_at":
                continue
            if not isinstance(value, datetime) or timezone.is_naive(value):
                raise ValidationError({name: "Use a date and time in the laboratory time zone."})
        if sample.collected_at > sample.received_at:
            raise ValidationError({"collected_at": "Collection must not follow receipt."})
        if sample.collection_type == "composite" and sample.collection_started_at is None:
            raise ValidationError({"collection_started_at": "Composite collection needs a start."})
        if sample.collection_type == "grab" and sample.collection_started_at is not None:
            raise ValidationError(
                {"collection_started_at": "Grab samples have no composite start."}
            )
    if section == "observations":
        _check_observations(sample)
    sample.full_clean()
    after = {key: value for key, value in snapshot(sample).items() if key in SECTIONS[section]}
    if all(before[key] == value for key, value in after.items()):
        raise ValidationError("Change at least one receipt detail before saving a correction.")
    sample.revision += 1
    sample.save(update_fields=[*SECTIONS[section], "revision"])
    after["revision"] = sample.revision
    record_event(
        actor=actor,
        action=f"sample.{section}_corrected",
        obj=sample,
        before=before,
        after=after,
        reason=reason,
    )
    return sample


def _check_observations(sample):
    for name in ("seal_condition", "preservation_check"):
        value = (getattr(sample, name) or "").strip()
        if not value:
            raise ValidationError({name: "This receipt observation is required."})
        setattr(sample, name, value)
    sample.temperature_instrument = (sample.temperature_instrument or "").strip()
    sample.discrepancy = (sample.discrepancy or "").strip()
    if sample.discrepancy and not sample.disposition_note.strip():
        raise ValidationError(
            {
                "discrepancy": "A discrepancy needs a disposition note. Use Change disposition "
                "first to record the decision and its reason."
            }
        )
    temperature = sample.cooler_temperature
    if not isinstance(temperature, Decimal) or not temperature.is_finite():
        raise ValidationError({"cooler_temperature": "Temperature must be a finite decimal."})
    original = (sample.cooler_temperature_original or "").strip() or str(temperature)
    try:
        matches = Decimal(original) == temperature
    except InvalidOperation, ValueError:
        matches = False
    if not matches:
        raise ValidationError(
            {"cooler_temperature": "The reading as read must match the recorded value."}
        )
    sample.cooler_temperature_original = original


def _test_labels(definitions):
    return [test_option_data(d)["text"] for d in definitions]


def _requested(sample):
    return list(
        RequestedTest.objects.filter(sample=sample)
        .select_related("definition__analyte", "definition__method")
        .order_by("pk")
    )


def _tests_state(sample, requested, revision):
    definitions = [r.definition for r in requested]
    return {
        "definition_ids": [d.pk for d in definitions],
        "tests": _test_labels(definitions),
        "matrix": sample.matrix,
        "revision": revision,
    }


def _check_definitions(definitions, matrix):
    if not definitions:
        raise ValidationError("Select at least one requested test.")
    if len({d.pk for d in definitions}) != len(definitions):
        raise ValidationError("Each test can be requested once.")
    if any(d.matrix != matrix or not d.is_active or not d.method.is_active for d in definitions):
        raise ValidationError("Requested tests must be active and match the sample matrix.")


def _delete_requested(requested):
    try:
        for item in requested:
            item.delete()
    except ProtectedError as exc:
        raise ValidationError(
            "A test with analytical or external work cannot be removed from the sample."
        ) from exc


def unremovable_tests(sample):
    """{requested_test pk: reason} for tests that already carry work and must stay."""
    locked = {}
    for pk in Subcontract.objects.filter(requested_test__sample=sample).values_list(
        "requested_test_id", flat=True
    ):
        locked[pk] = "Sent to an external laboratory"
    for pk in Result.objects.filter(requested_test__sample=sample).values_list(
        "requested_test_id", flat=True
    ):
        locked[pk] = "Has an analytical work record"
    for dependency in preparation_dependencies(sample):
        for pk in dependency["requested_test_ids"]:
            locked.setdefault(
                pk,
                f"It is part of preparation {dependency['aliquot_label']} "
                f"in {dependency['batch_number']}",
            )
    qc_methods = set(
        QCSample.objects.filter(parent_sample=sample).values_list("batch__method_id", flat=True)
    )
    for pk, method_id in RequestedTest.objects.filter(sample=sample).values_list(
        "pk", "definition__method_id"
    ):
        if method_id in qc_methods:
            locked.setdefault(pk, "Its sample is the parent of a QC sample for this method")
    return locked


@transaction.atomic
def change_requested_tests(*, actor, sample_id, expected_revision, reason, add=(), remove=()):
    """Add tests to, or withdraw never-started tests from, an already received sample.

    Holding clocks keep running from collection; a test added late is evaluated like any other
    and shows as at risk or expired. Nothing issued is rewritten, so adding is allowed after
    report issue; the new result simply needs its own review and report."""
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    add, remove = list(add), {getattr(r, "pk", r) for r in remove}
    if not add and not remove:
        raise ValidationError("Choose at least one test to add or remove.")
    sample = _lock_sample(sample_id, expected_revision)
    if blocker := correction_blocker(sample, "tests"):
        raise ValidationError(blocker)
    if add and sample.disposition == Sample.Disposition.REJECT:
        raise ValidationError(
            "This sample was rejected. Change its disposition before adding tests."
        )
    current = _requested(sample)
    by_pk = {r.pk: r for r in current}
    if remove - set(by_pk):
        raise ValidationError("A test to remove is not requested on this sample.")
    locked = unremovable_tests(sample)
    if blocked := [pk for pk in remove if pk in locked]:
        raise ValidationError(
            f"{by_pk[blocked[0]].definition.analyte.name} cannot be removed. {locked[blocked[0]]}."
        )
    have = {r.definition_id for r in current}
    if any(d.pk in have for d in add):
        raise ValidationError("A selected test is already requested on this sample.")
    kept = [r.definition for r in current if r.pk not in remove]
    _check_definitions(kept + add, sample.matrix)
    before = _tests_state(sample, current, sample.revision)
    _delete_requested([by_pk[pk] for pk in remove])
    RequestedTest.objects.bulk_create([RequestedTest(sample=sample, definition=d) for d in add])
    sample.revision += 1
    sample.save(update_fields=["revision"])
    after = _tests_state(sample, _requested(sample), sample.revision)
    record_event(
        actor=actor,
        action="sample.tests_changed",
        obj=sample,
        before=before,
        after=after,
        reason=reason,
    )
    return sample


@transaction.atomic
def correct_sample_matrix(*, actor, sample_id, expected_revision, matrix, tests, reason):
    """Fix a wrongly recorded matrix before any work exists, replacing the requested tests."""
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    sample = _lock_sample(sample_id, expected_revision)
    if blocker := correction_blocker(sample, "matrix"):
        raise ValidationError(blocker)
    if matrix not in Sample.Matrix.values:
        raise ValidationError({"matrix": "Choose a valid matrix."})
    if matrix == sample.matrix:
        raise ValidationError({"matrix": "Choose a different matrix to correct it."})
    tests = list(tests)
    _check_definitions(tests, matrix)
    current = _requested(sample)
    before = {**_tests_state(sample, current, sample.revision), "source_type": sample.source_type}
    _delete_requested(current)
    sample.matrix = matrix
    if not Sample.source_matches(matrix, sample.source_type):
        # A source belongs to its matrix; record the right one with a sample-details correction.
        sample.source_type = ""
    sample.revision += 1
    sample.full_clean()
    sample.save(update_fields=["matrix", "source_type", "revision"])
    RequestedTest.objects.bulk_create([RequestedTest(sample=sample, definition=d) for d in tests])
    after = {
        **_tests_state(sample, _requested(sample), sample.revision),
        "source_type": sample.source_type,
    }
    record_event(
        actor=actor,
        action="sample.matrix_corrected",
        obj=sample,
        before=before,
        after=after,
        reason=reason,
    )
    return sample


@transaction.atomic
def correct_order_reference(*, actor, order_id, expected_revision, coc_reference, reason):
    """Correct the chain-of-custody reference of an order, while no issued report shows it."""
    require_role(actor, *RECEIPT_ROLES)
    reason = _reason(reason)
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.revision != expected_revision:
        raise ValidationError("Order changed. Reload and review its current record.")
    if blocker := order_reference_blocker(order):
        raise ValidationError(blocker)
    coc_reference = (coc_reference or "").strip()
    if not coc_reference:
        raise ValidationError({"coc_reference": "A chain of custody reference is required."})
    if coc_reference == order.coc_reference:
        raise ValidationError({"coc_reference": "Change the reference before saving."})
    before = {"coc_reference": order.coc_reference, "revision": order.revision}
    order.coc_reference = coc_reference
    order.revision += 1
    order.full_clean()
    order.save(update_fields=["coc_reference", "revision"])
    record_event(
        actor=actor,
        action="order.reference_corrected",
        obj=order,
        before=before,
        after={"coc_reference": order.coc_reference, "revision": order.revision},
        reason=reason,
    )
    return order


def order_reference_blocker(order):
    if _issued(order.samples.values_list("lab_id", flat=True)):
        return (
            "An issued report shows this chain of custody reference. The original report and "
            "receipt remain available; this path cannot change released records."
        )
    return ""
