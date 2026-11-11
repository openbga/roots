"""Method detection limit rules (40 CFR Part 136 Appendix B, Revision 2) as pure Decimal functions.

Every rule cites its section. The function never returns a partial MDL: when the data do not meet
the procedure the outcome is "not_evaluated" with every reason listed. Calculations use the
original Decimal observations; nothing is rounded except the final storage quantum (10 places).
"""

import calendar
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from .stats import TValueUnavailable, t_quantile_99

MIN_SPIKES = 7  # II.2(b)
MIN_BLANKS = 7  # II.2(b)
MIN_BATCHES = 3  # II.2(b)
MIN_DATES = 3  # II.2(b)
WINDOW_MONTHS = 24  # II.2(b), II.4(b)
RECENT_BLANK_MONTHS = 6  # II.4(e)
RECENT_BLANK_COUNT = 50  # II.4(e)
RECALCULATE_MONTHS = 13  # II.4(a)
PERCENTILE_MIN_BLANKS = 100  # II.2(d)(iii)(B): more than 100 blanks
STORE = Decimal(1).scaleb(-10)

CALCULATED, NOT_EVALUATED = "calculated", "not_evaluated"


@dataclass(frozen=True)
class Obs:
    pk: int
    kind: str  # "spike" or "blank"
    value: Decimal | None  # None: not detected (not a numerical result)
    qualitative_ok: bool | None
    spike_level: Decimal | None
    prepared_on: date
    analyzed_on: date
    analyzed_at: datetime
    batch: str
    preparation: str = ""
    instrument: str = ""
    prepared_assumed: bool = False


@dataclass
class Outcome:
    status: str = NOT_EVALUATED
    reasons: list = field(default_factory=list)
    spike_n: int = 0  # analyses (the statistics use every analysis, II.2(d)(i))
    blank_n: int = 0
    spike_preparations: int = 0  # distinct preparations or extracts (the minimums, II.2(b)(iii))
    blank_preparations: int = 0
    spike_level: Decimal | None = None
    spike_mean: Decimal | None = None
    spike_sd: Decimal | None = None
    mean_recovery_pct: Decimal | None = None
    t_spike: Decimal | None = None
    t_spike_source: str = ""
    mdl_s: Decimal | None = None
    blank_mean: Decimal | None = None
    blank_sd: Decimal | None = None
    t_blank: Decimal | None = None
    t_blank_source: str = ""
    mdl_b: Decimal | None = None
    mdl_b_method: str = ""
    mdl_b_note: str = ""
    verified_mdl: Decimal | None = None
    existing_mdl: Decimal | None = None
    ratio_to_existing: Decimal | None = None
    blanks_above_existing: int | None = None
    retention_eligible: bool | None = None


def months_before(moment, months):
    index = moment.year * 12 + moment.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return moment.replace(
        year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1])
    )


def months_after(moment, months):
    return months_before(moment, -months)


def select_blank_window(blanks, as_of, window):
    """Blanks for MDL_b (II.4(e)): all from the last 24 months, or optionally the last six months or
    the fifty most recent, whichever gives more. Returns the chosen list (never edits data)."""
    pool = [b for b in blanks if months_before(as_of, WINDOW_MONTHS) <= b.analyzed_at <= as_of]
    if window != "recent":
        return pool
    last_six = [b for b in pool if b.analyzed_at >= months_before(as_of, RECENT_BLANK_MONTHS)]
    fifty = sorted(pool, key=lambda b: (b.analyzed_at, b.pk), reverse=True)[:RECENT_BLANK_COUNT]
    return last_six if len(last_six) >= len(fifty) else fifty


def _quantize(value):
    return None if value is None else value.quantize(STORE, rounding=ROUND_HALF_UP)


def preparation_count(observations):
    """Distinct preparations: one extract analysed on several instruments counts once
    (II.2(b)(iii))."""
    return len({(o.batch.strip().lower(), o.preparation.strip().lower()) for o in observations})


def _prep_dates(observations):
    """Preparation dates that were actually recorded; an assumed date proves nothing."""
    return {o.prepared_on for o in observations if not o.prepared_assumed}


def _spread(label, observations, reasons):
    """II.2(b): at least three batches, prepared on three dates and analysed on three dates.
    Applied to the spiked samples and to the method blanks each (fails closed; see evidence)."""
    batches = {o.batch.strip().lower() for o in observations if o.batch.strip()}
    prepared = _prep_dates(observations)
    assumed = sum(1 for o in observations if o.prepared_assumed)
    analyzed = {o.analyzed_on for o in observations}
    if len(batches) < MIN_BATCHES:
        reasons.append(
            f"{label}: {len(batches)} batch(es); at least {MIN_BATCHES} batches are required "
            "(II.2(b))."
        )
    if len(prepared) < MIN_DATES:
        extra = (
            f" {assumed} observation(s) have no recorded preparation time (the analysis time was "
            "assumed) and cannot show the spread; record the actual preparation times."
            if assumed
            else ""
        )
        reasons.append(
            f"{label}: prepared on {len(prepared)} recorded calendar date(s); at least "
            f"{MIN_DATES} are required (II.2(b)).{extra}"
        )
    if len(analyzed) < MIN_DATES:
        reasons.append(
            f"{label}: analysed on {len(analyzed)} calendar date(s); at least {MIN_DATES} are "
            "required (II.2(b))."
        )


def _instrument_rule(spikes, blanks, reasons):
    """II.2(b)(i)-(ii): with several instruments, each needs two spiked samples and two method
    blanks prepared and analysed on different calendar dates."""
    names = {o.instrument.strip() for o in [*spikes, *blanks]}
    if len(names) < 2:
        return
    if "" in names:
        reasons.append(
            "Several instruments are used but some observations have no instrument; record the "
            "instrument on every observation (II.2(b)(i))."
        )
    for name in sorted(names - {""}):
        for label, rows in (("spiked", spikes), ("blank", blanks)):
            mine = [o for o in rows if o.instrument.strip() == name]
            prep_dates, analysis_dates = len(_prep_dates(mine)), len({o.analyzed_on for o in mine})
            if len(mine) < 2 or prep_dates < 2 or analysis_dates < 2:
                reasons.append(
                    f"Instrument {name}: needs at least 2 {label} samples prepared and analysed on "
                    f"different calendar dates; has {len(mine)} with {prep_dates} recorded "
                    f"preparation and {analysis_dates} analysis date(s) (II.2(b)(ii))."
                )


def _failing_spike_reason(kind, bad, total):
    """Initial: repeat at a higher level (II.2(c)). Verification: the 5% review threshold
    (II.3(c)(i)); within it Appendix B is silent on how such a spike enters the standard
    deviation, so the calculation stays closed and says so."""
    if kind != "verification":
        return (
            f"{bad} of {total} spiked sample(s) did not give a numerical result greater than zero "
            "or did not meet qualitative identification; repeat the spikes at a higher "
            "concentration (II.2(c)), or exclude a documented gross failure."
        )
    pct = (Decimal(bad) * 100 / Decimal(total)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    head = (
        f"{bad} of {total} spiked sample(s) ({pct}%) did not return a positive numerical result "
        "meeting qualitative identification. "
    )
    if bad * 100 > 5 * total:
        return head + (
            "That is more than 5%, so the spiking level must be increased and the initial MDL "
            "re-determined (II.3(c)(i))."
        )
    return head + (
        "That is within the 5% spiking-level threshold (II.3(c)(i)), but Appendix B does not say "
        "how such a result enters the standard deviation for ongoing verification. Exclude it "
        "only if it is a documented gross failure (II.4(b)); otherwise the quality system must "
        "decide before this can be calculated."
    )


def _blank_limit(blanks):
    """MDL_b by case (II.2(d)(iii)). Returns (value, method, note, mean, sd, t, t_source)."""
    numeric = [b.value for b in blanks if b.value is not None]
    n = len(blanks)
    if not numeric:
        return (
            None,
            "not_applicable",
            "No method blank gave a numerical result (A).",
            None,
            None,
            None,
            "",
        )
    if len(numeric) < n:
        if n > PERCENTILE_MIN_BLANKS:
            ranked = [None] * (n - len(numeric)) + sorted(numeric)  # not detected ranks lowest
            rank = int((Decimal(n) * Decimal("0.99")).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            chosen = ranked[rank - 1]
            if chosen is not None:
                note = f"Rank {rank} of {n} (n x 0.99 rounded half up) (B)."
                return chosen, "percentile", note, None, None, None, ""
            note = (
                f"Rank {rank} of {n} is a not-detected result; the highest numerical blank is "
                "used (B)."
            )
            return max(numeric), "highest", note, None, None, None, ""
        return max(numeric), "highest", "Highest method blank result (B).", None, None, None, ""
    mean = statistics.mean(numeric)
    sd = statistics.stdev(numeric)
    t, source = t_quantile_99(len(numeric))
    mean_used = Decimal(0) if mean < 0 else mean
    note = "Mean (zero if negative) plus t x standard deviation (C)."
    return mean_used + t * sd, "mean_t", note, mean, sd, t, source


def calculate(spikes, blanks, *, study_level, as_of, existing_mdl=None, kind="initial"):
    """Compute MDL_s, MDL_b and the verified MDL, or explain why not. `spikes` and `blanks` are
    the non-excluded observations; the caller has already applied the blank window. `kind` is the
    study kind: retention of an existing MDL (II.4(f)) belongs to ongoing verification only."""
    existing_mdl = existing_mdl if kind == "verification" else None
    out = Outcome(
        spike_n=len(spikes),
        blank_n=len(blanks),
        spike_preparations=preparation_count(spikes),
        blank_preparations=preparation_count(blanks),
        existing_mdl=existing_mdl,
    )
    reasons = out.reasons
    for label, rows, prepared, minimum in (
        ("spiked sample(s)", spikes, out.spike_preparations, MIN_SPIKES),
        ("method blank(s)", blanks, out.blank_preparations, MIN_BLANKS),
    ):
        if prepared < minimum:
            reasons.append(
                f"{prepared} distinct preparation(s) of {label} from {len(rows)} analyses; at "
                f"least {minimum} preparations are required, and one extract analysed on "
                "several instruments counts once (II.2(b), II.2(b)(iii))."
            )
    bad = [s for s in spikes if s.value is None or s.value <= 0 or not s.qualitative_ok]
    if bad:
        reasons.append(_failing_spike_reason(kind, len(bad), len(spikes)))
    if any(s.spike_level != study_level for s in spikes):
        reasons.append("Spiked samples have different spiking levels; use one level (II.4(b)).")
    stale = [s for s in spikes if s.analyzed_at < months_before(as_of, WINDOW_MONTHS)]
    if stale:
        reasons.append(
            f"{len(stale)} spiked sample(s) are older than {WINDOW_MONTHS} months (II.2(b))."
        )
    if spikes:
        _spread("Spiked samples", spikes, reasons)
    if blanks:
        _spread("Method blanks", blanks, reasons)
    _instrument_rule(spikes, blanks, reasons)
    if reasons:
        return out

    values = [s.value for s in spikes]
    out.spike_level = study_level
    out.spike_mean = statistics.mean(values)
    out.spike_sd = statistics.stdev(values)
    out.mean_recovery_pct = out.spike_mean / study_level * 100
    try:
        out.t_spike, out.t_spike_source = t_quantile_99(len(values))
        out.mdl_s = out.t_spike * out.spike_sd
        (
            out.mdl_b,
            out.mdl_b_method,
            out.mdl_b_note,
            out.blank_mean,
            out.blank_sd,
            out.t_blank,
            out.t_blank_source,
        ) = _blank_limit(blanks)
    except TValueUnavailable as exc:
        reasons.append(str(exc))
        return out
    out.verified_mdl = out.mdl_s if out.mdl_b is None else max(out.mdl_s, out.mdl_b)
    if existing_mdl is not None and existing_mdl > 0:
        out.ratio_to_existing = out.verified_mdl / existing_mdl
        above = sum(1 for b in blanks if b.value is not None and b.value > existing_mdl)
        out.blanks_above_existing = above
        out.retention_eligible = Decimal("0.5") <= out.ratio_to_existing <= Decimal(
            "2.0"
        ) and above * 100 < 3 * len(blanks)
    for name in (
        "spike_mean",
        "spike_sd",
        "mean_recovery_pct",
        "mdl_s",
        "blank_mean",
        "blank_sd",
        "mdl_b",
        "verified_mdl",
    ):
        setattr(out, name, _quantize(getattr(out, name)))
    out.ratio_to_existing = (
        None
        if out.ratio_to_existing is None
        else out.ratio_to_existing.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    )
    out.status = CALCULATED
    return out
