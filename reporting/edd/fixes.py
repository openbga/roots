"""Where each NYSDEC finding is resolved. One grouping for the released-version preview and the
draft readiness page, so both name the same causes with the same links.

A released version's data is frozen, so its data findings need a new version. A draft's data is
still correctable at its source (the sample, the catalog), so draft findings link there."""

from dataclasses import dataclass

from django.urls import reverse

from analytical.models import Qualifier

RELEASED_LABELS = {
    "project": "Project EDD codes",
    "test": "Test mapping",
    "qualifier": "Qualifier mapping",
    "context": "Export context",
    "catalog": "Catalog, then a new version",
    "version": "Released data: needs a new version",
    "unsupported": "Not supported yet",
}
DRAFT_LABELS = {
    "project": "Project EDD codes",
    "test": "Test mapping",
    "qualifier": "Qualifier mapping",
    "catalog": "Analyte catalog",
    "sample": "Sample record",
    "uneditable": "Not correctable here",
    "result": "Result record",
    "record": "Source record",
    "unsupported": "Not supported yet",
}
FIX_ORDER = (
    "project",
    "test",
    "qualifier",
    "context",
    "catalog",
    "sample",
    "result",
    "record",
    "uneditable",
    "version",
    "unsupported",
)
# Sample fields the receipt "identity" correction can change (client sample ID, collector).
IDENTITY_FIELDS = {"sys_sample_code", "sample_name", "sampler"}


@dataclass(frozen=True)
class Place:
    """Where findings are shown. `context_url` is the released version's export-context page
    (None for a draft: the context cannot be entered before release). `draft` selects links to
    correctable source records. `samples` maps lab ID -> (sample pk, order pk) for a draft."""

    project_id: int
    here: str
    context_url: str | None = None
    draft: bool = False
    samples: dict | None = None
    blocked: dict | None = None  # lab ID -> why its identity cannot be corrected
    can_configure: bool = True
    can_receive: bool = True  # may use receipt's sample correction (custodian, project manager)


def fix_target(issue, place, codes):
    """(kind, url) of the page that resolves an issue; url None when nothing can be linked."""
    kind = issue.target[0] if issue.target else None
    if kind == "context":
        return kind, place.context_url
    if kind in ("project", "test", "qualifier") and not place.can_configure:
        return kind, None
    if kind == "project":
        url = reverse("reporting:nysdec-project", args=[place.project_id])
        return kind, f"{url}?next={place.here}"
    if kind == "test":
        return kind, f"{reverse('reporting:nysdec-test', args=[issue.target[1]])}?next={place.here}"
    if kind == "qualifier" and issue.target[1] in codes:
        url = reverse("reporting:nysdec-qualifier", args=[codes[issue.target[1]]])
        return kind, f"{url}?next={place.here}"
    if kind == "result":  # a draft result is corrected with a new revision; an issued one is not
        if place.draft:
            return kind, reverse("analytical:result-detail", args=[issue.target[1]])
        return "version", None
    if issue.field == "cas_rn":
        return "catalog", reverse("catalog:analyte-list")
    if "subcontracted" in issue.message:
        return "unsupported", None
    if not place.draft:
        return "version", None
    sample = (place.samples or {}).get(issue.lab_id)
    if sample is None:
        return "record", None
    sample_pk = sample
    if issue.field == "chain_of_custody":
        return "uneditable", None
    if issue.field in IDENTITY_FIELDS:
        if (place.blocked or {}).get(issue.lab_id):
            return "uneditable", None
        if not place.can_receive:  # the correction page would only refuse; show the record
            return "sample", reverse("receipt:sample-detail", args=[sample_pk])
        return "sample", reverse("receipt:sample-correct", args=[sample_pk, "identity"])
    return "sample", reverse("receipt:sample-detail", args=[sample_pk])


def issue_groups(issues, place):
    """Identical causes grouped into one row: problem, field, fix destination, affected records.
    Every issue stays visible (count + full record list); nothing is dropped."""
    labels = DRAFT_LABELS if place.draft else RELEASED_LABELS
    codes = dict(Qualifier.objects.values_list("code", "pk"))
    groups = {}
    for issue in issues:
        kind, url = fix_target(issue, place, codes)
        note = _note(issue, place, kind)
        key = (kind, url, issue.field, issue.message, note)
        group = groups.setdefault(
            key,
            {
                "kind": kind,
                "label": labels.get(kind, "Context after release"),
                "url": url,
                "field": issue.field,
                "message": issue.message,
                "note": note,
                "records": [],
            },
        )
        group["records"].append(issue.record)
    ordered = sorted(
        groups.values(), key=lambda g: (FIX_ORDER.index(g["kind"]), -len(g["records"]), g["field"])
    )
    return ordered, summarize(ordered)


def summarize(ordered):
    """One entry per fix destination kind: affected record count and number of causes."""
    by_kind = {}
    for g in ordered:
        entry = by_kind.setdefault(
            g["kind"],
            {"kind": g["kind"], "label": g["label"], "url": g["url"], "issues": 0, "causes": 0},
        )
        entry["issues"] += len(g["records"])
        entry["causes"] += 1
    return list(by_kind.values())


def _note(issue, place, kind):
    """Why a draft finding cannot be corrected here, or who can correct it."""
    if kind == "uneditable":
        if issue.field == "chain_of_custody":
            return "The chain of custody reference is fixed when the order is created."
        return (place.blocked or {}).get(issue.lab_id, "")
    if kind == "sample" and issue.field in IDENTITY_FIELDS and not place.can_receive:
        return "Correcting the sample ID needs the custodian or project manager role."
    return ""
