"""When a dated obligation falls due: the one rule behind every due pill ({% due_pill %}).

Used for training expiry, nonconformance due dates, lot expiry, equipment calibration and
maintenance, and chain-of-custody delivery targets. Holding times have their own clock
(analytical.holding). Dates are compared in the laboratory's time zone (settings.TIME_ZONE).
"""

from datetime import date, datetime, timedelta

from django.utils import timezone

TONES = {"overdue": "danger", "soon": "warning", "ok": "success", "none": "neutral"}


def due_state(due, today=None, soon_days=30, now=None):
    """('overdue'|'soon'|'ok'|'none', label).

    A date is due through that day and overdue after it. A date and time (a delivery target) is
    overdue once that moment has passed; its days are counted in laboratory dates."""
    now = now or timezone.now()
    today = today or timezone.localdate(now)
    if due is None:
        return "none", "Not scheduled"
    if isinstance(due, datetime):
        if due < now:
            days = (today - timezone.localdate(due)).days
            return "overdue", f"Overdue {days} d" if days else "Overdue"
        due = timezone.localdate(due)
    elif not isinstance(due, date):
        raise TypeError(f"due_state needs a date or datetime, not {type(due).__name__}")
    if due < today:
        return "overdue", f"Overdue {(today - due).days} d"
    if due <= today + timedelta(days=soon_days):
        return "soon", "Due today" if due == today else f"Due in {(due - today).days} d"
    return "ok", "Scheduled"
