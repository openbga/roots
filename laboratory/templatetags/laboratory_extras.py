from django import template

from ..selectors import batch_context, due_state, lot_state
from ..training import State as TrainingState

register = template.Library()


@register.filter
def qty(value):
    """Exact decimal quantity without exponent or trailing zeros."""
    if value is None or value == "":
        return "—"
    return format(value.normalize(), "f") if hasattr(value, "normalize") else str(value)


@register.filter
def training_label(state):
    """Display text of a training record's computed state (current, expired, ...)."""
    return TrainingState.LABELS.get(state, state)


@register.simple_tag
def lot_status(lot, today=None):
    label, tone = lot_state(lot, today)
    return {"label": label, "tone": tone}


@register.simple_tag
def due_status(due, today=None, label_prefix=""):
    kind, label = due_state(due, today)
    tone = {"overdue": "danger", "soon": "warning", "ok": "success", "none": "neutral"}[kind]
    return {"kind": kind, "label": label, "tone": tone}


@register.filter
def action_label(code):
    """Readable history title for an audit action code: "lot.received" -> "Lot received"."""
    return str(code or "").replace(".", " ").replace("_", " ").capitalize()


@register.filter
def short_hash(value):
    return (value or "")[:12]


@register.inclusion_tag("laboratory/batch_context.html")
def laboratory_context(user, batch=None, sample=None, equipment=()):
    """Laboratory records beside a batch or sample, for information only."""
    return {**batch_context(user, batch=batch, sample=sample, equipment=equipment)}
