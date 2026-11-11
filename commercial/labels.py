"""Planned bottle labels for a client submission. They say PLANNED and carry their own PLN- code:
no lab accession number and no claim that anything was received, collected or preserved."""

from django.core.exceptions import ValidationError

from receipt.labels import Label


def _short(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _collected(sample):
    if sample.collected_at:
        return f"{sample.collected_at:%Y-%m-%d} (client-declared)"
    if sample.planned_collection_on:
        return f"not yet, planned {sample.planned_collection_on:%Y-%m-%d}"
    return "not yet"


def planned_labels(submission):
    """One label per declared container, in sample order."""
    labels = []
    samples = submission.samples.prefetch_related("containers").order_by("position", "pk")
    for sample in samples:
        total = sum(c.quantity for c in sample.containers.all())
        number = 0
        for container in sample.containers.all():
            for _ in range(container.quantity):
                number += 1
                detail = _short(container.description, 40)
                if container.preservation:
                    detail += f" · {_short(container.preservation, 40)}"
                labels.append(
                    Label(
                        code=f"PLN-{submission.pk}-{sample.pk}-{number:02d}",
                        heading=f"PLANNED · {submission.number} · bottle {number} of {total}",
                        sample_id=_short(sample.client_sample_id, 60),
                        detail=f"Not received, not a lab ID · {detail}",
                        collected=_collected(sample),
                    )
                )
    if not labels:
        raise ValidationError("This submission declares no containers to label.")
    return labels
