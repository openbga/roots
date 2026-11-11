"""QC control charts built from existing QC measurements (read only).

Two kinds of limit are kept apart and labelled as such:

* configured limits: the lab's own QCLimit rows. They decide QC status at approval and release.
* statistical bands: mean +/- 2 and +/- 3 sample standard deviations (n - 1 divisor, no c4 bias
  correction) of the chosen recent points, as they are: nothing checks that the process was in
  statistical control during them, and the bands are recomputed on every view. They describe the
  process, never accept or reject anything, and never replace or certify a configured limit.

Plotted and tabulated values are the exact Decimals the QC evaluation computes. Rendering is done by
Chart.js in the browser from `chart_payload`; floats there are display-only positions.
"""

import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

from .models import QCLimit, QCMeasure, QCMeasurement, ResultKind
from .qc import APPLICABLE_MEASURES, evaluate_measurement, evaluate_stored, plain

# At least 20 points before bands are drawn. MARLAP Chapter 18 Procedure 18.1 says "ideally at
# least 20"; EPA 815-R-05-004 section 7.2.8 says "usually 20 to 30" for laboratory fortified blank
# recoveries. This is a display threshold of this application, not a regulatory requirement.
MIN_BASELINE_POINTS = 20
BASELINE_CHOICES = (20, 30, 50, 0)  # 0 = every point
UNIT_FOR = {QCMeasure.RECOVERY: "%", QCMeasure.RPD: "%"}
NON_DETECT_REASON = "Non-detect (not a numerical result, not plotted)"


@dataclass
class Point:
    measurement_id: int
    qc_sample: str
    batch: str
    when: object
    observed_text: str
    value: Decimal
    configured_status: str  # pass, fail or not_evaluated against configured limits
    in_baseline: bool = False
    band: str = ""  # "", "warning" (beyond 2 SD) or "control" (beyond 3 SD); statistical only


@dataclass
class Series:
    definition: object
    qc_type: str
    measure: str
    unit: str
    points: list = field(default_factory=list)
    not_plotted: dict = field(default_factory=dict)  # reason -> count
    configured: tuple | None = None  # (lower, upper) of the lab's active limit
    stats: dict | None = None
    stats_reason: str = ""
    baseline: int = 30

    @property
    def non_detect_count(self):
        """Non-detects left out of a value chart and its bands (the application's own handling)."""
        return self.not_plotted.get(NON_DETECT_REASON, 0)

    @property
    def counts(self):
        return {
            "points": len(self.points),
            "outside_configured": sum(p.configured_status == "fail" for p in self.points),
            "unevaluated": sum(p.configured_status == "not_evaluated" for p in self.points),
            "warning": sum(p.band in ("warning", "control") for p in self.points),
            "control": sum(p.band == "control" for p in self.points),
        }


def measures_for(qc_type):
    return list(APPLICABLE_MEASURES[qc_type])


def _computed(measurement, measure):
    """The measure's value from the existing evaluation code with no limits applied."""
    free = {m: (None, None) for m in APPLICABLE_MEASURES[measurement.qc_sample.qc_type]}
    evaluation = evaluate_measurement(
        measurement.qc_sample.qc_type,
        kind=measurement.kind,
        observed=measurement.observed_value,
        reporting_limit=measurement.reporting_limit_applied,
        spike=measurement.spike_value,
        native=measurement.native_value,
        reference=measurement.reference_value,
        limits=free,
    )
    return next((m.computed for m in evaluation.measures if m.measure == measure), None)


def build_series(definition, qc_type, measure, baseline=30, phase="analysis"):
    series = Series(
        definition=definition,
        qc_type=qc_type,
        measure=measure,
        unit=UNIT_FOR.get(measure, definition.unit),
        baseline=baseline,
    )
    limit = QCLimit.objects.filter(
        definition=definition, qc_type=qc_type, measure=measure, is_active=True, phase=phase
    ).first()
    series.configured = (limit.lower, limit.upper) if limit else None
    measurements = (
        QCMeasurement.objects.filter(
            definition=definition,
            qc_sample__qc_type=qc_type,
            is_current=True,
            qc_sample__batch__kind="preparation" if phase == "preparation" else "analytical",
        )
        .select_related("qc_sample__batch")
        .order_by("analysis_started_at", "pk")
    )
    cache = {}

    def skip(reason):
        series.not_plotted[reason] = series.not_plotted.get(reason, 0) + 1

    for m in measurements:
        if measure == QCMeasure.VALUE:
            value = m.observed_value if m.kind == ResultKind.DETECTED else None
            if value is None:
                skip(NON_DETECT_REASON)
                continue
        else:
            value = _computed(m, measure)
            if value is None:
                skip("Not computable (non-detect or missing spike/reference input)")
                continue
        status = next(
            (x.status for x in evaluate_stored(m, cache).measures if x.measure == measure),
            "not_evaluated",
        )
        series.points.append(
            Point(
                m.pk,
                m.qc_sample.label,
                m.qc_sample.batch.number,
                m.analysis_started_at,
                m.observed_text,
                value,
                status,
            )
        )
    _statistics(series)
    return series


def _statistics(series):
    baseline = series.points if not series.baseline else series.points[-series.baseline :]
    n = len(baseline)
    if n < MIN_BASELINE_POINTS:
        series.stats_reason = (
            f"Statistical bands are not evaluated: the baseline has {n} point(s); common practice "
            f"is at least {MIN_BASELINE_POINTS} (descriptive guidance, not a regulatory threshold)."
        )
        return
    values = [p.value for p in baseline]
    mean, sd = statistics.mean(values), statistics.stdev(values)
    series.stats = {
        "n": n,
        "mean": mean,
        "sd": sd,
        "warning": (mean - 2 * sd, mean + 2 * sd),
        "control": (mean - 3 * sd, mean + 3 * sd),
    }
    in_baseline = {p.measurement_id for p in baseline}
    for p in series.points:
        p.in_baseline = p.measurement_id in in_baseline
        distance = abs(p.value - mean)
        p.band = "control" if distance > 3 * sd else "warning" if distance > 2 * sd else ""


# --------------------------------------------------------------------------- Chart.js payload

MAX_PLOTTED = 100


def chart_payload(series):
    """Display data for the Chart.js initializer (static/analytical/control_chart.js).

    The float conversions here are display-only: they give Chart.js positions. Every value a
    person reads (tooltips, the data table) is the exact Decimal text from `plain`, and nothing
    here is stored or compared."""
    points = series.points[-MAX_PLOTTED:]
    lines = []
    if series.configured:
        lower, upper = series.configured
        if lower is not None:
            lines.append(("Configured lower limit", "config", lower))
        if upper is not None:
            lines.append(("Configured upper limit", "config", upper))
    if series.stats:
        stats = series.stats
        lines += [
            ("Mean", "mean", stats["mean"]),
            ("Mean +2 SD (descriptive)", "warning", stats["warning"][1]),
            ("Mean -2 SD (descriptive)", "warning", stats["warning"][0]),
            ("Mean +3 SD (descriptive)", "control", stats["control"][1]),
            ("Mean -3 SD (descriptive)", "control", stats["control"][0]),
        ]
    return {
        "unit": series.unit,
        "labels": [f"{timezone.localtime(p.when):%Y-%m-%d %H:%M}" for p in points],  # lab zone
        "points": [
            {
                "y": float(p.value),
                "exact": plain(p.value),
                "sample": p.qc_sample,
                "batch": p.batch,
                "observed": p.observed_text,
                # square: outside the lab's configured limits; triangle: beyond 3 SD (descriptive)
                "shape": "rect"
                if p.configured_status == "fail"
                else "triangle"
                if p.band == "control"
                else "circle",
                "band": p.band,
                "status": p.configured_status,
            }
            for p in points
        ],
        "lines": [
            {"label": label, "kind": kind, "y": float(value), "exact": plain(value)}
            for label, kind, value in lines
        ],
    }
