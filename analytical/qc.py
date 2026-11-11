"""Deterministic QC evaluation. No acceptance thresholds live here: limits and frequencies come
from QCLimit and QCRequirement rows, and anything not configured is reported as not evaluated."""

import hashlib
import json
from dataclasses import dataclass, field, replace
from decimal import Decimal, localcontext

from django.utils import timezone

from catalog.models import TestDefinition

from .models import (
    PreparedTest,
    QCCorrectiveAction,
    QCLimit,
    QCMeasure,
    QCMeasurement,
    QCRequirement,
    QCSample,
    QCType,
    Result,
    ResultKind,
    ResultRevision,
)

PASS, FAIL, NOT_EVALUATED = "pass", "fail", "not_evaluated"

# Which measures are evaluated for each QC type. Each needs a configured limit.
APPLICABLE_MEASURES = {
    QCType.METHOD_BLANK: (QCMeasure.VALUE,),
    QCType.LCS: (QCMeasure.RECOVERY,),
    QCType.CALIBRATION_VERIFICATION: (QCMeasure.RECOVERY,),
    QCType.MATRIX_SPIKE: (QCMeasure.RECOVERY,),
    QCType.MATRIX_SPIKE_DUPLICATE: (QCMeasure.RECOVERY, QCMeasure.RPD),
    QCType.LAB_DUPLICATE: (QCMeasure.RPD,),
}
NEEDS_SPIKE = {
    QCType.LCS,
    QCType.CALIBRATION_VERIFICATION,
    QCType.MATRIX_SPIKE,
    QCType.MATRIX_SPIKE_DUPLICATE,
}
NEEDS_NATIVE = {QCType.MATRIX_SPIKE, QCType.MATRIX_SPIKE_DUPLICATE}
NEEDS_REFERENCE = {QCType.LAB_DUPLICATE, QCType.MATRIX_SPIKE_DUPLICATE}
NEEDS_PARENT = {QCType.MATRIX_SPIKE, QCType.MATRIX_SPIKE_DUPLICATE, QCType.LAB_DUPLICATE}

_DISPLAY = Decimal("0.000001")
_BLANK_ND_PASS = "Non-detect with reporting limit within the limit."
_BLANK_ND_FAIL = (
    "Non-detect, but the reporting limit applied exceeds the blank limit or no upper limit is set."
)


def plain(value):
    """Decimal as plain text for descriptions: at most six places, no trailing zeros.

    Display only. Comparisons and stored values keep the exact Decimal."""
    return format(value.quantize(_DISPLAY).normalize(), "f")


def trim(value):
    """Exact Decimal as plain text without storage-only trailing zeros (0.0100000000 -> 0.01).
    Never rounds: the numeric value is unchanged. Display and form-initial use only."""
    return format(value.normalize(), "f")


def _range_text(lower, upper):
    if lower is not None and upper is not None:
        return f"{plain(lower)} to {plain(upper)}"
    if lower is not None:
        return f"at least {plain(lower)}"
    return f"at most {plain(upper)}"


@dataclass(frozen=True)
class MeasureEvaluation:
    measure: str
    status: str
    computed: Decimal | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    detail: str = ""

    def as_json(self):
        def text(x):
            return None if x is None else format(x, "f")

        return {
            "measure": self.measure,
            "status": self.status,
            "computed": None
            if self.computed is None
            else format(self.computed.quantize(_DISPLAY), "f"),
            "lower": text(self.lower),
            "upper": text(self.upper),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Evaluation:
    status: str
    measures: tuple[MeasureEvaluation, ...] = ()

    @property
    def summary(self):
        parts = []
        for m in self.measures:
            if m.status == PASS:
                shown = None if m.computed is None else plain(m.computed)
                parts.append(
                    f"{m.measure} {shown} within limits" if shown else f"{m.measure}: {m.detail}"
                )
            else:
                parts.append(f"{m.measure}: {m.detail}")
        return "; ".join(parts) or "No evaluation"

    def as_json(self):
        return {"status": self.status, "measures": [m.as_json() for m in self.measures]}


def _within(value, lower, upper):
    return (lower is None or value >= lower) and (upper is None or value <= upper)


def _check(measure, computed, bounds, label):
    lower, upper = bounds
    ok = _within(computed, lower, upper)
    detail = (
        f"{label} {plain(computed)} is within limits"
        if ok
        else f"{label} {plain(computed)} is outside limits ({_range_text(lower, upper)})"
    )
    return MeasureEvaluation(measure, PASS if ok else FAIL, computed, lower, upper, detail)


def evaluate_measurement(
    qc_type, *, kind, observed, reporting_limit, spike, native, reference, limits
):
    """Evaluate one QC observation. `limits` maps measure -> (lower, upper) for configured,
    active limits. Pure and deterministic: the same inputs always give the same result."""
    results = []
    with localcontext() as ctx:
        ctx.prec = 50
        for measure in APPLICABLE_MEASURES[qc_type]:
            bounds = limits.get(measure)
            if bounds is None:
                results.append(
                    MeasureEvaluation(
                        measure, NOT_EVALUATED, detail=f"No {measure} limit is configured."
                    )
                )
                continue
            lower, upper = bounds
            if measure == QCMeasure.VALUE:
                if kind == ResultKind.NON_DETECT:
                    ok = upper is not None and reporting_limit <= upper
                    results.append(
                        MeasureEvaluation(
                            measure,
                            PASS if ok else FAIL,
                            None,
                            lower,
                            upper,
                            _BLANK_ND_PASS if ok else _BLANK_ND_FAIL,
                        )
                    )
                else:
                    results.append(_check(measure, observed, bounds, "Measured value"))
            elif measure == QCMeasure.RECOVERY:
                if kind == ResultKind.NON_DETECT:
                    results.append(
                        MeasureEvaluation(
                            measure,
                            NOT_EVALUATED,
                            detail="Recovery cannot be computed from a non-detect.",
                        )
                    )
                elif spike is None or spike <= 0:
                    results.append(
                        MeasureEvaluation(
                            measure, NOT_EVALUATED, detail="A positive spike amount is required."
                        )
                    )
                elif qc_type in NEEDS_NATIVE and native is None:
                    results.append(
                        MeasureEvaluation(
                            measure, NOT_EVALUATED, detail="The unspiked sample result is missing."
                        )
                    )
                else:
                    base = native if qc_type in NEEDS_NATIVE else Decimal(0)
                    results.append(
                        _check(measure, (observed - base) / spike * 100, bounds, "Recovery %")
                    )
            else:  # RPD
                if kind == ResultKind.NON_DETECT or reference is None:
                    results.append(
                        MeasureEvaluation(
                            measure,
                            NOT_EVALUATED,
                            detail="Relative percent difference cannot be "
                            "computed (non-detect or no reference "
                            "value).",
                        )
                    )
                elif observed == reference:
                    results.append(_check(measure, Decimal(0), bounds, "RPD %"))
                else:
                    mean = abs(observed + reference) / 2
                    if mean == 0:
                        results.append(
                            MeasureEvaluation(
                                measure, NOT_EVALUATED, detail="Mean of the pair is zero."
                            )
                        )
                    else:
                        results.append(
                            _check(measure, abs(observed - reference) / mean * 100, bounds, "RPD %")
                        )
    statuses = {m.status for m in results}
    overall = FAIL if FAIL in statuses else NOT_EVALUATED if NOT_EVALUATED in statuses else PASS
    return Evaluation(overall, tuple(results))


def active_limit_bounds(definition_id, qc_type, cache=None, phase="analysis"):
    key = (definition_id, qc_type, phase)
    if cache is not None and key in cache:
        return cache[key]
    rows = QCLimit.objects.filter(
        definition_id=definition_id, qc_type=qc_type, is_active=True, phase=phase
    )
    bounds = {row.measure: (row.lower, row.upper) for row in rows}
    if cache is not None:
        cache[key] = bounds
    return bounds


def evaluate_stored(measurement, cache=None):
    qc_type = measurement.qc_sample.qc_type
    return evaluate_measurement(
        qc_type,
        kind=measurement.kind,
        observed=measurement.observed_value,
        reporting_limit=measurement.reporting_limit_applied,
        spike=measurement.spike_value,
        native=measurement.native_value,
        reference=measurement.reference_value,
        limits=active_limit_bounds(
            measurement.definition_id,
            qc_type,
            cache,
            phase="preparation"
            if measurement.qc_sample.batch.kind == "preparation"
            else "analysis",
        ),
    )


# --------------------------------------------------------------------- batch status


def _digest(value):
    """Short stable fingerprint of a JSON-able structure, used to bind a disposition to the
    exact finding (values, limits) it was recorded for."""
    text = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:10]


@dataclass(frozen=True)
class QCIssue:
    """One QC finding. `key` identifies the finding *and its evaluated context*: a disposition
    recorded for an earlier context (other limits, other counts) no longer matches."""

    key: str
    code: str  # shortfall, missing_measurement, failed, not_evaluated
    qc_type: str
    message: str
    qc_sample_id: int | None = None
    measurement_id: int | None = None
    definition_id: int | None = None
    action: QCCorrectiveAction | None = None
    # Set when an acceptance exists but was recorded by someone with a stake in the data.
    conflict: str = ""
    qc_sample_label: str = ""

    @property
    def link_label(self):
        """Names the finding for an action link: 'LCS-1 (Laboratory control sample)'."""
        kind = QCType(self.qc_type).label
        return f"{self.qc_sample_label} ({kind})" if self.qc_sample_label else f"{kind} frequency"

    @property
    def resolved(self):
        return (
            self.action is not None
            and not self.conflict
            and self.action.disposition == QCCorrectiveAction.Disposition.ACCEPT_QUALIFIED
        )

    @property
    def blocking(self):
        return not self.resolved


@dataclass
class BatchQC:
    """QC state of one batch, loaded once. Use for the batch page, review and approval."""

    batch: object
    requirements: list = field(default_factory=list)
    qc_samples: list = field(default_factory=list)
    measurements: dict = field(default_factory=dict)  # (qc_sample_id, definition_id) -> measurement
    actions: dict = field(default_factory=dict)  # target_key -> latest action
    field_sample_count: int = 0
    definition_counts: dict = field(default_factory=dict)
    prepared_parent_definitions: set = field(default_factory=set)
    cancelled_definition_counts: dict = field(default_factory=dict)
    # definition_id -> ids of users who entered or submitted any revision in this batch
    analysts: dict = field(default_factory=dict)
    _limits: dict = field(default_factory=dict)
    _evaluations: dict = field(default_factory=dict)

    @classmethod
    def load(cls, batch):
        self = cls(batch)
        self.requirements = list(
            QCRequirement.objects.filter(method_id=batch.method_id, is_active=True)
        )
        self.qc_samples = list(QCSample.objects.filter(batch=batch))
        for m in QCMeasurement.objects.filter(
            qc_sample__batch=batch, is_current=True
        ).select_related("qc_sample__batch", "analysis_batch", "entered_by"):
            self.measurements[(m.qc_sample_id, m.definition_id)] = m
        for a in (
            QCCorrectiveAction.objects.filter(batch=batch)
            .select_related("recorded_by")
            .order_by("pk")
        ):
            self.actions[a.target_key] = a
        # Historical membership counts: moving results to another batch (reanalysis) must not
        # erase this batch's QC obligations.
        samples = set(
            Result.objects.filter(batch=batch).values_list("requested_test__sample_id", flat=True)
        )
        for rev in ResultRevision.objects.filter(batch=batch).values_list(
            "result__requested_test__sample_id", "definition_id", "entered_by_id", "submitted_by_id"
        ):
            samples.add(rev[0])
            self.analysts.setdefault(rev[1], set()).update(u for u in rev[2:] if u)
        self.field_sample_count = len(samples)
        if batch.kind == "preparation":
            members = list(
                PreparedTest.objects.filter(aliquot__batch=batch).values_list(
                    "requested_test__definition_id",
                    "aliquot_id",
                    "aliquot__sample_id",
                    "aliquot__cancelled_at",
                )
            )
            counts, cancelled = {}, {}
            for definition_id, aliquot_id, sample_id, cancelled_at in members:
                counts.setdefault(definition_id, set()).add(aliquot_id)
                self.prepared_parent_definitions.add((sample_id, definition_id))
                if cancelled_at:
                    cancelled.setdefault(definition_id, set()).add(aliquot_id)
            self.definition_counts = {key: len(value) for key, value in counts.items()}
            self.field_sample_count = len({a for _, a, _, _ in members})
            self.cancelled_definition_counts = {key: len(value) for key, value in cancelled.items()}
            for definition_id, entered, submitted in ResultRevision.objects.filter(
                prepared_aliquot__batch=batch
            ).values_list("definition_id", "entered_by_id", "submitted_by_id"):
                self.analysts.setdefault(definition_id, set()).update(
                    u for u in (entered, submitted) if u
                )
        return self

    def evaluation(self, measurement):
        if measurement.pk not in self._evaluations:
            self._evaluations[measurement.pk] = evaluate_stored(measurement, self._limits)
        return self._evaluations[measurement.pk]

    def samples_for_definition(self, definition=None):
        if self.batch.kind != "preparation" or definition is None:
            return self.qc_samples
        return [
            sample
            for sample in self.qc_samples
            if not sample.parent_sample_id
            or (sample.parent_sample_id, definition.pk) in self.prepared_parent_definitions
        ]

    def shortfalls(self, definition=None):
        by_type = {}
        for s in self.samples_for_definition(definition):
            by_type[s.qc_type] = by_type.get(s.qc_type, 0) + 1
        out = []
        for req in self.requirements:
            count = (
                self.definition_counts.get(definition.pk, 0)
                if definition and self.batch.kind == "preparation"
                else self.field_sample_count
            )
            need = req.required_count(count)
            have = by_type.get(req.qc_type, 0)
            out.append((req, need, have))
        return out

    def acceptance_conflict(self, user_id, issue):
        """Why `user_id` may not accept this finding, or '' if they may. A person cannot
        waive QC they measured, or QC affecting results they entered or submitted."""
        measurement = next(
            (m for m in self.measurements.values() if m.pk == issue.measurement_id), None
        )
        if measurement is not None and measurement.entered_by_id == user_id:
            return "recorded the QC measurement"
        definitions = (
            [issue.definition_id] if issue.definition_id is not None else list(self.analysts)
        )
        if any(user_id in self.analysts.get(d, ()) for d in definitions):
            return "entered or submitted results this QC finding affects"
        return ""

    def _issue(self, **kwargs):
        issue = QCIssue(**kwargs)
        action = issue.action
        if (
            action is not None
            and action.disposition == QCCorrectiveAction.Disposition.ACCEPT_QUALIFIED
        ):
            why = self.acceptance_conflict(action.recorded_by_id, issue)
            if why:
                issue = replace(issue, conflict=f"{action.recorded_by.get_username()} {why}")
        return issue

    def issues(self, definition):
        """All QC issues relevant to results of `definition` in this batch."""
        issues = []
        for req, need, have in self.shortfalls(definition):
            if have < need:
                key = (
                    f"s:{req.qc_type}:{need}:{have}:{self.field_sample_count}:"
                    f"{req.min_per_batch}:{req.every_n_samples or 0}"
                )
                if self.batch.kind == "preparation":
                    key = f"p:{definition.pk}:{self.definition_counts.get(definition.pk, 0)}:{key}"
                issues.append(
                    self._issue(
                        key=key,
                        code="shortfall",
                        qc_type=req.qc_type,
                        definition_id=definition.pk if self.batch.kind == "preparation" else None,
                        message=f"{req.get_qc_type_display()}: {need} required, {have} in batch.",
                        action=self.actions.get(key),
                    )
                )
        for sample in self.samples_for_definition(definition):
            m = self.measurements.get((sample.pk, definition.pk))
            if m is None:
                key = f"x:{sample.pk}:{definition.pk}"
                issues.append(
                    self._issue(
                        key=key,
                        code="missing_measurement",
                        qc_type=sample.qc_type,
                        message=(
                            f"{sample.label}: no measurement recorded for {definition.analyte}."
                        ),
                        qc_sample_id=sample.pk,
                        qc_sample_label=sample.label,
                        definition_id=definition.pk,
                        action=self.actions.get(key),
                    )
                )
                continue
            ev = self.evaluation(m)
            if ev.status != PASS:
                key = f"m:{m.pk}:{_digest(ev.as_json())}"
                issues.append(
                    self._issue(
                        key=key,
                        code="failed" if ev.status == FAIL else "not_evaluated",
                        qc_type=sample.qc_type,
                        message=f"{sample.label} {definition.analyte}: {ev.summary}",
                        qc_sample_id=sample.pk,
                        qc_sample_label=sample.label,
                        measurement_id=m.pk,
                        definition_id=definition.pk,
                        action=self.actions.get(key),
                    )
                )
        return issues

    def definitions(self):
        """Test definitions this batch has results, revisions or QC measurements for."""
        ids = set(self.analysts) | {d for _, d in self.measurements}
        ids.update(self.definition_counts)
        ids |= set(
            Result.objects.filter(batch=self.batch).values_list(
                "requested_test__definition_id", flat=True
            )
        )
        return list(TestDefinition.objects.filter(pk__in=ids).select_related("analyte"))

    def blocked_definition_ids(self):
        """Ids of tests with at least one blocking QC issue in this batch (whole batch)."""
        return {d.pk for d in self.definitions() if any(i.blocking for i in self.issues(d))}

    def all_issues(self):
        """Every distinct issue in the batch across its definitions, keyed by `key`."""
        found = {}
        for definition in self.definitions():
            for issue in self.issues(definition):
                found.setdefault(issue.key, issue)
        return list(found.values())

    def evidence(self, definition):
        """JSON snapshot of the QC relied on for results of `definition`."""
        measurements = []
        for sample in self.samples_for_definition(definition):
            m = self.measurements.get((sample.pk, definition.pk))
            if m is None:
                continue
            ev = self.evaluation(m)
            measurements.append(
                {
                    "measurement_id": m.pk,
                    "qc_sample": sample.label,
                    "qc_type": sample.qc_type,
                    "analyte": m.analyte_name,
                    "unit": m.unit,
                    "observed_text": m.observed_text,
                    "reporting_limit_applied": format(m.reporting_limit_applied, "f"),
                    "spike_value": None if m.spike_value is None else format(m.spike_value, "f"),
                    "native_value": None if m.native_value is None else format(m.native_value, "f"),
                    "reference_value": None
                    if m.reference_value is None
                    else format(m.reference_value, "f"),
                    "analysis_started_at": m.analysis_started_at.isoformat(),
                    **(
                        {"analysis_batch": m.analysis_batch.number if m.analysis_batch_id else None}
                        if self.batch.kind == "preparation"
                        else {}
                    ),
                    "evaluation": ev.as_json(),
                }
            )
        issues = [
            {
                "key": i.key,
                "code": i.code,
                "qc_type": i.qc_type,
                "message": i.message,
                "resolved": i.resolved,
                "conflict": i.conflict,
                "disposition": i.action.disposition if i.action else None,
                "note": i.action.note if i.action else "",
                "recorded_by": i.action.recorded_by.get_username() if i.action else None,
                "recorded_at": i.action.recorded_at.isoformat() if i.action else None,
            }
            for i in self.issues(definition)
        ]
        return {
            "batch": self.batch.number,
            "method": str(self.batch.method),
            "evaluated_at": timezone.now().isoformat(),
            "field_samples": self.definition_counts.get(definition.pk, 0)
            if self.batch.kind == "preparation"
            else self.field_sample_count,
            "cancelled_aliquots": self.cancelled_definition_counts.get(definition.pk, 0),
            "requirements": [
                {"qc_type": r.qc_type, "required": need, "present": have}
                for r, need, have in self.shortfalls(definition)
            ],
            "measurements": measurements,
            "issues": issues,
        }
