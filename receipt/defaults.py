"""Reusable intake defaults for "Receive another using these defaults".

Defaults are read server-side from a sample already received on the same order, never from
client-supplied values. Only an allowlist of shared, editable descriptors is copied. Everything
observed about the new sample (client sample ID, sample type, remarks, collection place/times,
receipt time, temperature, coolant, seal/container/preservation checks, disposition, location) and
every accession or custody event stays with the source sample and must be entered deliberately.
"""

from .models import Sample
from .widgets import test_option_data

CONTAINER_DEFAULTS = ("description", "preservation")  # Container condition is an observation.


def sample_defaults(source):
    """Return form initial values, container rows and a display summary for `source`.

    Tests that are no longer active (or whose method is retired) are not copied; the summary
    says so, and the normal receipt validation still runs on whatever the user submits.
    """
    handoff = source.custody_events.filter(kind="receipt").order_by("occurred_at").first()
    definitions = [
        item.definition
        for item in source.requested_tests.select_related(
            "definition__method", "definition__analyte"
        )
    ]
    usable = [
        d for d in definitions if d.is_active and d.method.is_active and d.matrix == source.matrix
    ]
    containers = [
        {name: getattr(container, name) for name in CONTAINER_DEFAULTS}
        for container in source.containers.order_by("label")
    ]
    initial = {
        "matrix": source.matrix,
        "source_type": source.source_type,
        "collector_name": source.collector_name,
        "tests": [d.pk for d in usable],
    }
    if handoff:
        initial["relinquished_by"] = handoff.relinquished_by
    reused = [
        ("Matrix", Sample.Matrix(source.matrix).label),
        ("Collector", source.collector_name),
    ]
    if source.source_type:
        reused.insert(1, ("Sample source", source.get_source_type_display()))
    if handoff:
        reused.append(("Relinquished by", handoff.relinquished_by))
    reused.append(
        ("Requested tests", "; ".join(test_option_data(d)["text"] for d in usable) or "None")
    )
    reused.append(
        (
            "Containers",
            "; ".join(
                f"{row['description']}"
                + (f" ({row['preservation']})" if row["preservation"] else "")
                for row in containers
            ),
        )
    )
    skipped = len(definitions) - len(usable)
    return {
        "initial": initial,
        "containers": containers[:20],
        "reused": reused,
        "skipped_tests": skipped,
        "source": source,
    }
