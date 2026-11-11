"""Database reads for exports: immutable revision facts referenced by a frozen snapshot."""

from analytical.models import QCMeasurement, ResultRevision

from .nysdec import LAB_QC_TYPES, QCFacts, RevisionFacts


def revision_facts(snapshot):
    """{revision id: RevisionFacts} for every result in the snapshot. Revisions are immutable once
    approved, so these facts cannot drift from what was released."""
    ids = [
        r["revision_id"]
        for s in snapshot["content"]["samples"]
        for r in s["results"]
        if r.get("revision_id")  # subcontracted rows have no internal revision
    ]
    revisions = list(
        ResultRevision.objects.filter(pk__in=ids).select_related(
            "definition__method", "definition__analyte"
        )
    )
    first = {
        rid: (started, aliquot)
        for rid, started, aliquot in ResultRevision.objects.filter(
            result_id__in=[r.result_id for r in revisions], number=1
        ).values_list("result_id", "analysis_started_at", "prepared_aliquot_id")
    }
    return {
        r.pk: RevisionFacts(r.definition_id, str(r.definition), *first[r.result_id])
        for r in revisions
    }


def qc_facts(snapshot):
    """{QC measurement id: QCFacts} for exported lab QC in the snapshot's frozen QC evidence.
    Measurements are superseded, never edited, so definition and analysis start are stable."""
    ids = [
        m["measurement_id"]
        for batch in snapshot["content"]["batches"]
        for evidence in batch["qc"].values()
        for m in evidence.get("measurements", [])
        if m.get("qc_type") in LAB_QC_TYPES and not m.get("withheld")
    ]
    out = {}
    for m in QCMeasurement.objects.filter(pk__in=ids).select_related("supersedes"):
        earlier = m.supersedes
        out[m.pk] = QCFacts(
            m.definition_id,
            bool(earlier and earlier.analysis_started_at != m.analysis_started_at),
        )
    return out
