"""Holding-time evaluation from configured rules (start event, end event, duration)."""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .models import HoldingTimeRule, ResultRevision


@dataclass(frozen=True)
class HoldingCheck:
    rule_id: int
    name: str
    start_event: str
    end_event: str
    start_at: datetime | None
    deadline: datetime | None
    end_at: datetime | None
    # met/exceeded: both ends known. within/at_risk/expired: clock still running.
    status: str
    detail: str = ""

    def as_json(self):
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "start_event": self.start_event,
            "end_event": self.end_event,
            "status": self.status,
            "detail": self.detail,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
        }


def evaluate_rule(rule, *, collected_at, prepared_at=None, analysis_at=None, now=None):
    """Evaluate one rule. The limit is time to the *start* of the end event, so an end exactly
    at the deadline is met. With no end timestamp and `now`, the running clock is assessed."""
    start_at = collected_at if rule.start_event == "collection" else prepared_at
    end_at = prepared_at if rule.end_event == "preparation" else analysis_at

    def make(status, deadline=None, detail=""):
        return HoldingCheck(
            rule.pk,
            rule.name,
            rule.start_event,
            rule.end_event,
            start_at,
            deadline,
            end_at,
            status,
            detail,
        )

    if start_at is None:
        return make("not_evaluable", detail="Preparation start time is not recorded.")
    deadline = start_at + rule.duration
    if end_at is None:
        if now is None:
            return make(
                "not_evaluable", deadline, f"{rule.end_event.title()} start is not recorded."
            )
        if now > deadline:
            return make("expired", deadline)
        if rule.warning_window is not None and deadline - now <= rule.warning_window:
            return make("at_risk", deadline)
        return make("within", deadline)
    if end_at < start_at:
        return make(
            "not_evaluable",
            deadline,
            "End time precedes the start event; correct the recorded chronology.",
        )
    if end_at > deadline:
        return make("exceeded", deadline, f"Exceeded by {timedelta_label(end_at - deadline)}.")
    return make("met", deadline)


def for_analyte(rules, analyte):
    """Narrow holding rules to the active ones that apply to one analyte's test.

    A rule that names the analyte applies. A method-wide rule (no analyte) applies unless an
    active rule of the same method, matrix, start event and end event names the analyte: the
    analyte's own limit replaces the method-wide one for that clock and leaves other clocks
    alone. `analyte` is an Analyte, its id, or an OuterRef to one when the rules annotate
    another queryset (selectors.with_holding_deadline)."""
    own = HoldingTimeRule.objects.filter(
        method=OuterRef("method"),
        matrix=OuterRef("matrix"),
        start_event=OuterRef("start_event"),
        end_event=OuterRef("end_event"),
        analyte=OuterRef(analyte) if isinstance(analyte, OuterRef) else analyte,
        is_active=True,
    )
    return rules.filter(is_active=True).filter(
        Q(analyte=analyte) | Q(analyte__isnull=True) & ~Exists(own)
    )


def active_rules(method, matrix, analyte):
    """Active rules that apply to one test of the method and matrix."""
    return list(for_analyte(HoldingTimeRule.objects.filter(method=method, matrix=matrix), analyte))


def evaluate_for_entry(
    *, method, matrix, analyte, collected_at, prepared_at, analysis_at, extra_method_ids=()
):
    """Evaluate every active rule that applies to an entered result of one analyte.
    Returns (status, list of JSON dicts)."""
    rules = list(
        for_analyte(
            HoldingTimeRule.objects.filter(
                method_id__in={method.pk, *extra_method_ids}, matrix=matrix
            ),
            analyte,
        )
    )
    if not rules:
        return ResultRevision.HoldingStatus.NOT_EVALUATED, []
    checks = [
        evaluate_rule(
            r, collected_at=collected_at, prepared_at=prepared_at, analysis_at=analysis_at
        )
        for r in rules
    ]
    statuses = {c.status for c in checks}
    if "exceeded" in statuses:
        overall = ResultRevision.HoldingStatus.EXCEEDED
    elif "not_evaluable" in statuses:
        overall = ResultRevision.HoldingStatus.NOT_EVALUABLE
    else:
        overall = ResultRevision.HoldingStatus.MET
    return overall, [c.as_json() for c in checks]


def running_clocks(method, matrix, analyte, *, collected_at, prepared_at=None, now=None):
    """Clocks for a test not yet analysed (worklist). Worst status first."""
    now = now or timezone.now()
    order = {"expired": 0, "at_risk": 1, "not_evaluable": 2, "within": 3}
    checks = [
        evaluate_rule(r, collected_at=collected_at, prepared_at=prepared_at, now=now)
        for r in active_rules(method, matrix, analyte)
    ]
    return sorted(checks, key=lambda c: (order.get(c.status, 9), c.deadline or now))


def duration_label(delta):
    """Whole-unit lab wording for a configured limit: '15 min', '48 h', '14 days'."""
    seconds = int(delta.total_seconds())
    if seconds % 60:
        return timedelta_label(delta)
    minutes = seconds // 60
    if minutes % 1440 == 0 and minutes >= 3 * 1440:
        days = minutes // 1440
        return f"{days} day{'s' if days != 1 else ''}"
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    return f"{minutes} min"


def timedelta_label(delta):
    """Compact label such as '3d 4h' or '12m' for a timedelta."""
    seconds = int(abs(delta.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def preparation_error(sample, prepared_at):
    """The composite collection event is its end, as on every holding clock."""
    if prepared_at is not None and prepared_at < sample.collected_at:
        prep = timezone.localtime(prepared_at).strftime("%Y-%m-%d %H:%M:%S %Z")
        collected = timezone.localtime(sample.collected_at).strftime("%Y-%m-%d %H:%M:%S %Z")
        return (
            f"Holding time cannot be evaluated: {sample.lab_id} preparation {prep} "
            "precedes collection "
            f"(composite end) {collected}. Correct the preparation time or batch assignment."
        )
    return ""
