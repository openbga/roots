"""Transactional MDL study workflow (40 CFR Part 136 Appendix B, Revision 2).

Analysts record observations and calculate; an independent reviewer decides. Every mutation writes
an attributed audit event. Calculations are immutable snapshots; configured reporting limits and QC
limits are never changed here.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.roles import require_role
from core.audit import record_event

from . import mdl
from .models import (
    MDLCalculation,
    MDLObservation,
    MDLStudy,
    PreparationRequirement,
    QCCorrectiveAction,
    QCMeasurement,
)
from .parsing import parse_decimal, parse_result_text
from .services import ANALYST, CLOCK_SKEW, REVIEWER

BLANK_WINDOWS = ("24_months", "recent")


def local_date(moment):
    """Calendar date in settings.TIME_ZONE (the lab's zone for all new input)."""
    return timezone.localtime(moment).date()


def current_calculation(definition_id):
    """Latest reviewed calculation that adopted or retained an MDL for the test, else None."""
    return (
        MDLCalculation.objects.filter(
            study__definition_id=definition_id, decision__in=["adopted", "retained"]
        )
        .order_by("-reviewed_at", "-pk")
        .first()
    )


def _lock_study(study_id):
    return (
        MDLStudy.objects.select_for_update().select_related("definition__analyte").get(pk=study_id)
    )


@transaction.atomic
def create_study(*, actor, definition, kind, spiking_level, instrument_note="", notes=""):
    require_role(actor, ANALYST)
    if kind not in MDLStudy.Kind.values:
        raise ValidationError({"kind": "Choose the kind of study."})
    if not definition.is_active:
        raise ValidationError({"definition": "This test definition is not active."})
    try:
        level = parse_decimal(spiking_level, "Spiking level")
        if level <= 0:
            raise ValidationError("Spiking level must be greater than zero.")
    except ValidationError as exc:
        raise ValidationError({"spiking_level": exc.messages}) from exc
    study = MDLStudy.objects.create(
        definition=definition,
        kind=kind,
        spiking_level=level,
        instrument_note=instrument_note.strip(),
        notes=notes,
        created_by=actor,
    )
    record_event(
        actor=actor,
        action="mdl.study_created",
        obj=study,
        after={
            "number": study.number,
            "test": str(definition),
            "kind": kind,
            "spiking_level": str(level),
        },
    )
    return study


@transaction.atomic
def add_observation(
    *,
    actor,
    study_id,
    kind,
    supplied_text,
    prepared_at,
    analyzed_at,
    batch_label,
    preparation_label,
    instrument="",
    spike_level="",
    qualitative_ok=None,
):
    require_role(actor, ANALYST)
    study = _lock_study(study_id)
    if study.status != MDLStudy.Status.OPEN:
        raise ValidationError("This study is closed.")
    errors = {}
    if kind not in MDLObservation.Kind.values:
        raise ValidationError({"kind": "Choose spiked sample or method blank."})
    result_kind = value = level = None
    try:
        result_kind, value = parse_result_text(supplied_text)
    except ValidationError as exc:
        errors["supplied_text"] = exc.messages
    if kind == "spike":
        try:
            level = parse_decimal(spike_level, "Spiking level")
            if level <= 0:
                raise ValidationError("Spiking level must be greater than zero.")
        except ValidationError as exc:
            errors["spike_level"] = exc.messages
        if qualitative_ok is None:
            errors["qualitative_ok"] = [
                "State whether the method's qualitative identification was met."
            ]
    now = timezone.now()
    if analyzed_at is None or analyzed_at > now + CLOCK_SKEW:
        errors["analyzed_at"] = ["Analysis time is required and cannot be in the future."]
    if prepared_at is None:
        errors["prepared_at"] = ["Preparation time is required."]
    elif analyzed_at is not None and prepared_at > analyzed_at:
        errors["prepared_at"] = ["Preparation cannot be after analysis."]
    if not (batch_label or "").strip():
        errors["batch_label"] = ["Name the preparation batch."]
    if not (preparation_label or "").strip():
        errors["preparation_label"] = [
            "Identify the preparation or extract (an extract analysed twice is one preparation)."
        ]
    if errors:
        raise ValidationError(errors)
    observation = MDLObservation.objects.create(
        study=study,
        kind=kind,
        supplied_text=supplied_text,
        result_kind=result_kind,
        value=value,
        spike_level=level,
        qualitative_ok=qualitative_ok if kind == "spike" else None,
        prepared_at=prepared_at,
        analyzed_at=analyzed_at,
        batch_label=batch_label.strip(),
        preparation_label=preparation_label.strip(),
        instrument=instrument.strip(),
        entered_by=actor,
    )
    record_event(
        actor=actor,
        action="mdl.observation_added",
        obj=observation,
        after={
            "study": study.number,
            "kind": kind,
            "supplied_text": supplied_text,
            "batch": observation.batch_label,
            "analyzed_at": analyzed_at.isoformat(),
        },
    )
    return observation


def blank_import_basis(definition_id):
    return (
        "preparation"
        if PreparationRequirement.objects.filter(
            definition_id=definition_id, is_active=True
        ).exists()
        else "analytical"
    )


@transaction.atomic
def import_blanks(*, actor, study_id, start, end, basis=None, reason=""):
    """Copy routine method-blank QC measurements for the test into the study, with provenance.
    Non-detects are kept as not-detected (never as zero). Already imported blanks are skipped, and
    so are blanks from batches whose QC disposition rejected the affected data: Appendix B
    II.4(d) includes all routine data except rejected batches whose samples were reanalysed.
    Returns (imported, skipped because the batch was rejected)."""
    require_role(actor, ANALYST)
    study = _lock_study(study_id)
    if study.status != MDLStudy.Status.OPEN:
        raise ValidationError("This study is closed.")
    if start is None or end is None or start > end:
        raise ValidationError("Give a start date that is not after the end date.")
    default_basis = blank_import_basis(study.definition_id)
    basis = basis or default_basis
    reason = (reason or "").strip()
    if basis not in {"preparation", "analytical"}:
        raise ValidationError({"basis": "Choose a single routine blank source basis."})
    if basis != default_basis and not reason:
        raise ValidationError(
            {
                "reason": (
                    "Explain why these alternative/combined blanks underwent every processing step "
                    "of the method, including any required preparation. "
                    "A batch label is not evidence."
                )
            }
        )
    taken = set(
        study.observations.exclude(source_measurement=None).values_list(
            "source_measurement_id", flat=True
        )
    )
    measurements = (
        QCMeasurement.objects.filter(
            definition=study.definition,
            qc_sample__qc_type="method_blank",
            qc_sample__batch__kind=basis,
            is_current=True,
            analysis_started_at__date__gte=start,
            analysis_started_at__date__lte=end,
        )
        .exclude(pk__in=taken)
        .select_related("qc_sample__batch", "analysis_batch")
        .order_by("analysis_started_at", "pk")
    )
    measurements = list(measurements)
    rejected = _rejected_batch_ids(
        {m.qc_sample.batch_id for m in measurements}
        | {m.analysis_batch_id for m in measurements if m.analysis_batch_id},
        study.definition_id,
    )
    created, skipped = [], 0
    for m in measurements:
        batch = m.qc_sample.batch
        if batch.pk in rejected or m.analysis_batch_id in rejected:
            skipped += 1
            continue
        assumed = batch.prepared_at is None or batch.prepared_at > m.analysis_started_at
        created.append(
            MDLObservation(
                study=study,
                kind="blank",
                supplied_text=m.observed_text,
                result_kind=m.kind,
                value=m.observed_value,
                prepared_at=m.analysis_started_at if assumed else batch.prepared_at,
                prepared_assumed=assumed,
                analyzed_at=m.analysis_started_at,
                batch_label=batch.number,
                preparation_label=m.qc_sample.label,
                entered_by=actor,
                source_measurement=m,
                source_context={
                    "basis": basis,
                    "source_batch_kind": batch.kind,
                    "source_batch": batch.number,
                    "analytical_batch": m.analysis_batch.number
                    if m.analysis_batch_id
                    else batch.number
                    if batch.kind == "analytical"
                    else None,
                    "reason": reason,
                },
            )
        )
    MDLObservation.objects.bulk_create(created)
    record_event(
        actor=actor,
        action="mdl.blanks_imported",
        obj=study,
        after={
            "count": len(created),
            "skipped_rejected_batches": skipped,
            "basis": basis,
            "reason": reason,
            "from": start.isoformat(),
            "to": end.isoformat(),
        },
    )
    return len(created), skipped


def _rejected_batch_ids(batch_ids, definition_id):
    """Batches where the latest disposition of a QC finding rejected data that affects this test.

    Only findings for the study's test, or batch-wide findings with no test (for example a
    shortfall), count. A rejection of another analyte's QC leaves these blanks routine data."""
    latest = {}
    # Not definition_id__in=[id, None]: Django drops None from IN lists, which would lose the
    # batch-wide findings. NULL needs its own IS NULL test.
    actions = QCCorrectiveAction.objects.filter(
        Q(definition_id=definition_id) | Q(definition_id__isnull=True), batch_id__in=batch_ids
    ).order_by("pk")
    for action in actions:
        latest[(action.batch_id, action.target_key)] = action.disposition
    return {batch for (batch, _), disposition in latest.items() if disposition == "reject_data"}


@transaction.atomic
def exclude_observation(*, actor, observation_id, reason):
    """Exclude a documented gross failure. Statistical outlier removal is not permitted
    (Appendix B II.2(b)); the rationale is kept on file and the observation is not altered."""
    require_role(actor, ANALYST, REVIEWER)
    # Lock order is study, then observation, so an exclusion serializes with calculate and review.
    study_id = MDLObservation.objects.values_list("study_id", flat=True).get(pk=observation_id)
    _lock_study(study_id)
    observation = (
        MDLObservation.objects.select_for_update().select_related("study").get(pk=observation_id)
    )
    if observation.study.status != MDLStudy.Status.OPEN:
        raise ValidationError("This study is closed.")
    if observation.excluded:
        raise ValidationError("This observation is already excluded.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError(
            {"reason": "Document the gross failure (for example an instrument malfunction)."}
        )
    observation.excluded, observation.exclusion_reason = True, reason
    observation.excluded_by, observation.excluded_at = actor, timezone.now()
    observation.save(update_fields=["excluded", "exclusion_reason", "excluded_by", "excluded_at"])
    record_event(
        actor=actor,
        action="mdl.observation_excluded",
        obj=observation,
        before={"excluded": False},
        after={"excluded": True},
        reason=reason,
    )
    return observation


def _as_obs(o):
    return mdl.Obs(
        pk=o.pk,
        kind=o.kind,
        value=o.value,
        qualitative_ok=o.qualitative_ok,
        spike_level=o.spike_level,
        prepared_on=local_date(o.prepared_at),
        analyzed_on=local_date(o.analyzed_at),
        analyzed_at=o.analyzed_at,
        batch=o.batch_label,
        preparation=o.preparation_label,
        instrument=o.instrument,
        prepared_assumed=o.prepared_assumed,
    )


def _snapshot(observation, used_ids):
    return {
        "id": observation.pk,
        "kind": observation.kind,
        "supplied_text": observation.supplied_text,
        "value": None if observation.value is None else format(observation.value, "f"),
        "spike_level": None
        if observation.spike_level is None
        else format(observation.spike_level, "f"),
        "qualitative_ok": observation.qualitative_ok,
        "prepared_at": observation.prepared_at.isoformat(),
        "prepared_assumed": observation.prepared_assumed,
        "analyzed_at": observation.analyzed_at.isoformat(),
        "batch": observation.batch_label,
        "preparation": observation.preparation_label,
        "instrument": observation.instrument,
        "entered_by_id": observation.entered_by_id,
        "entered_by": observation.entered_by.get_username(),
        "source_measurement_id": observation.source_measurement_id,
        "source_context": observation.source_context,
        "excluded": observation.excluded,
        "excluded_by_id": observation.excluded_by_id,
        "exclusion_reason": observation.exclusion_reason,
        "used": observation.pk in used_ids,
    }


def _evaluate(study, blank_window, as_of):
    """Build the inputs and run the pure calculation. Returns (outcome, rows, used ids)."""
    rows = list(study.observations.select_related("entered_by").order_by("analyzed_at", "pk"))
    active = [o for o in rows if not o.excluded]
    spikes = [_as_obs(o) for o in active if o.kind == "spike"]
    blanks = mdl.select_blank_window(
        [_as_obs(o) for o in active if o.kind == "blank"], as_of, blank_window
    )
    existing = current_calculation(study.definition_id)
    outcome = mdl.calculate(
        spikes,
        blanks,
        study_level=study.spiking_level,
        as_of=as_of,
        existing_mdl=existing.adopted_mdl if existing else None,
        kind=study.kind,
    )
    used = {s.pk for s in spikes} | {b.pk for b in blanks}
    return outcome, rows, used


def preview(study, blank_window="24_months"):
    """Readiness only: would a calculation be evaluated now, and if not, why. Nothing is saved
    and no MDL value is produced."""
    outcome, _, _ = _evaluate(study, blank_window, timezone.now())
    return outcome.status, outcome.reasons


@transaction.atomic
def calculate(*, actor, study_id, blank_window="24_months"):
    """Run the Appendix B calculation on the study's current data and keep the snapshot."""
    require_role(actor, ANALYST)
    study = _lock_study(study_id)
    if study.status != MDLStudy.Status.OPEN:
        raise ValidationError("This study is closed.")
    if blank_window not in BLANK_WINDOWS:
        raise ValidationError({"blank_window": "Choose a method blank window."})
    as_of = timezone.now()
    outcome, rows, used = _evaluate(study, blank_window, as_of)
    number = (
        study.calculations.order_by("-number").values_list("number", flat=True).first() or 0
    ) + 1
    fields = {
        name: getattr(outcome, name)
        for name in (
            "spike_n",
            "blank_n",
            "spike_preparations",
            "blank_preparations",
            "spike_level",
            "spike_mean",
            "spike_sd",
            "mean_recovery_pct",
            "t_spike",
            "t_spike_source",
            "mdl_s",
            "blank_mean",
            "blank_sd",
            "t_blank",
            "t_blank_source",
            "mdl_b",
            "mdl_b_method",
            "mdl_b_note",
            "verified_mdl",
            "existing_mdl",
            "ratio_to_existing",
            "blanks_above_existing",
            "retention_eligible",
        )
    }
    calculation = MDLCalculation.objects.create(
        study=study,
        number=number,
        status=outcome.status,
        reasons=outcome.reasons,
        calculated_by=actor,
        as_of=as_of,
        blank_window=blank_window,
        unit=study.definition.unit,
        inputs=[_snapshot(o, used) for o in rows],
        **fields,
    )
    record_event(
        actor=actor,
        action="mdl.calculated",
        obj=calculation,
        after={
            "study": study.number,
            "calculation": number,
            "status": outcome.status,
            "reasons": outcome.reasons,
            "verified_mdl": None if outcome.verified_mdl is None else str(outcome.verified_mdl),
        },
    )
    return calculation


def calculation_contributors(calculation):
    """Ids of everyone who shaped a calculation: who ran it, entered an input it used, or
    excluded an observation from it. None of them may review it."""
    contributors = {calculation.calculated_by_id}
    for row in calculation.inputs:
        if row["used"]:
            contributors.add(row["entered_by_id"])
        if row["excluded"] and row.get("excluded_by_id"):
            contributors.add(row["excluded_by_id"])
    return contributors


@transaction.atomic
def review_calculation(*, actor, calculation_id, decision, note=""):
    """Independent review. The reviewer must not have entered or excluded any input, or run the
    calculation; and only the study's latest calculation, on data unchanged since it ran."""
    require_role(actor, REVIEWER)
    # Lock order: study first (as every other mutation), then the calculation.
    study_id = MDLCalculation.objects.values_list("study_id", flat=True).get(pk=calculation_id)
    study = _lock_study(study_id)
    calculation = (
        MDLCalculation.objects.select_for_update()
        .select_related("study__definition")
        .get(pk=calculation_id)
    )
    if calculation.decision:
        raise ValidationError("This calculation has already been reviewed.")
    if actor.pk in calculation_contributors(calculation):
        raise PermissionDenied(
            "You calculated this MDL, entered data it uses, or excluded data from it, so you "
            "cannot review it."
        )
    if decision not in MDLCalculation.Decision.values:
        raise ValidationError({"decision": "Choose a decision."})
    note = (note or "").strip()
    if calculation.status != MDLCalculation.Status.CALCULATED:
        raise ValidationError("A calculation that was not evaluated cannot be adopted or kept.")
    newest = study.calculations.order_by("-number").values_list("pk", flat=True).first()
    if newest != calculation.pk:
        raise ValidationError(
            "A newer calculation exists for this study. Review the latest one; this one is out "
            "of date."
        )
    if study.observations.filter(
        Q(entered_at__gt=calculation.calculated_at) | Q(excluded_at__gt=calculation.calculated_at)
    ).exists():
        raise ValidationError(
            "Observations were added or excluded after this calculation ran. Calculate again so "
            "the review sees the current data."
        )
    adopted = None
    if decision == "adopted":
        adopted = calculation.verified_mdl
    elif decision == "retained":
        if study.kind != MDLStudy.Kind.VERIFICATION:
            raise ValidationError(
                "Keeping the existing MDL belongs to ongoing verification (Appendix B II.4(f)). "
                "An initial determination adopts its verified MDL or is rejected."
            )
        if not calculation.retention_eligible or calculation.existing_mdl is None:
            raise ValidationError(
                "The existing MDL can be kept only when the verified MDL is within 0.5 to 2.0 "
                "times it and fewer than 3% of blanks exceed it (II.4(f))."
            )
        adopted = calculation.existing_mdl
    if decision in ("retained", "rejected") and not note:
        raise ValidationError({"note": "Give the reason for this decision."})
    calculation.decision = decision
    calculation.reviewed_by, calculation.reviewed_at = actor, timezone.now()
    calculation.review_note, calculation.adopted_mdl = note, adopted
    calculation.save(
        update_fields=["decision", "reviewed_by", "reviewed_at", "review_note", "adopted_mdl"]
    )
    record_event(
        actor=actor,
        action="mdl.reviewed",
        obj=calculation,
        after={
            "study": calculation.study.number,
            "calculation": calculation.number,
            "decision": decision,
            "adopted_mdl": None if adopted is None else str(adopted),
            "unit": calculation.unit,
        },
        reason=note,
    )
    return calculation


@transaction.atomic
def close_study(*, actor, study_id):
    require_role(actor, ANALYST, REVIEWER)
    study = _lock_study(study_id)
    if study.status != MDLStudy.Status.OPEN:
        raise ValidationError("This study is already closed.")
    study.status, study.closed_by, study.closed_at = (
        MDLStudy.Status.CLOSED,
        actor,
        timezone.now(),
    )
    study.save(update_fields=["status", "closed_by", "closed_at"])
    record_event(
        actor=actor,
        action="mdl.study_closed",
        obj=study,
        before={"status": "open"},
        after={"status": "closed"},
    )
    return study
