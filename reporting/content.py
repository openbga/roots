"""Report content and release readiness, built from current records.

`assemble` reads a report's samples and their latest approved result revisions. Eligibility,
including current batch QC and holding-time state, comes from analytical.eligibility; the QC
summary is analytical's current BatchQC evidence. It returns the content that would be frozen
at release, a fingerprint of that content and the reasons it cannot be released. Preview,
release and the locked recheck all use it, so a release succeeds only for exactly the content
previewed.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from analytical.eligibility import reportable_results
from analytical.models import Result, ResultRevision
from analytical.preparation import preparation_snapshot
from analytical.preservation_services import preservation_blockers, preservation_snapshot
from analytical.qc import NEEDS_PARENT, BatchQC
from core.lab_profile import report_identity
from core.models import Lab
from receipt.models import Sample
from receipt.subcontracts import external_reportable_results

from .models import MAX_RESULTS, MAX_SAMPLES

SCHEMA = 1
UNREPORTABLE_DISPOSITIONS = ("hold", "reject")


def synthetic_notice():
    return getattr(settings, "REPORT_SYNTHETIC_DATA_NOTICE", True)


def person(user):
    if user is None:
        return None
    return user.get_full_name() or user.get_username()


def number_text(value):
    """Exact decimal as plain text without storage padding: 0.0500000000 -> 0.05."""
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def fingerprint(content):
    data = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


@dataclass
class ResultReadiness:
    analyte: str
    label: str
    problems: list
    result_id: int | None = None  # analytical Result to open for a held or blocked test
    batch_id: int | None = None
    subcontract_id: int | None = None  # receipt Subcontract for an externally routed test
    preservation_id: int | None = None
    preparation: dict | None = None


@dataclass
class SampleReadiness:
    sample: object
    tests: list = field(default_factory=list)  # ResultReadiness
    blockers: list = field(default_factory=list)

    @property
    def ready(self):
        return not self.blockers and not any(t.problems for t in self.tests)


@dataclass
class Assembly:
    content: dict
    fingerprint: str
    blockers: list  # report-level and per-test messages, flattened
    samples: list  # SampleReadiness, for the staff readiness table
    revision_ids: list
    batch_ids: list
    requested_tests: dict  # requested_test_id -> included approved revision id, or None
    report_blockers: list = field(default_factory=list)  # not tied to one sample
    external_tests: dict = field(default_factory=dict)  # requested_test_id -> SubcontractResult id
    issued_on: date | None = None  # local date whose laboratory identity the content holds

    @property
    def releasable(self):
        return not self.blockers


def _report_samples(report):
    return list(
        Sample.objects.filter(pk__in=report.samples.values("pk"))
        .select_related("order__project__client", "received_by")
        .prefetch_related("containers")
        .order_by("lab_id")
    )


OTHER_SAMPLE = "Sample not in this report"


def _scoped_qc(evidence, qc, client_id):
    """Analytical QC evidence as one client may see it.

    Lab controls and QC prepared from this client's samples keep their evidence. QC prepared
    from another client's sample, or with no recorded source sample, keeps its type, timing,
    pass/fail per measure, limits and disposition, but not its label, values or free text.
    """
    parents = {s.parent_sample_id for s in qc.qc_samples if s.parent_sample_id}
    owner = dict(
        Sample.objects.filter(pk__in=parents).values_list("pk", "order__project__client_id")
    )
    foreign = {
        s.pk: s.label
        for s in qc.qc_samples
        if (s.qc_type in NEEDS_PARENT or s.parent_sample_id)
        and owner.get(s.parent_sample_id) != client_id
    }
    labels = set(foreign.values())
    measurements, foreign_measurements = [], set()
    for m in evidence["measurements"]:
        if m["qc_sample"] not in labels:
            measurements.append({**m, "withheld": False})
            continue
        foreign_measurements.add(m["measurement_id"])
        measures = [
            {
                "measure": e["measure"],
                "status": e["status"],
                "computed": None,
                "lower": e["lower"],
                "upper": e["upper"],
                "detail": f"{e['measure']}: {e['status'].replace('_', ' ')}",
            }
            for e in m["evaluation"]["measures"]
        ]
        measurements.append(
            {
                **m,
                "qc_sample": OTHER_SAMPLE,
                "observed_text": None,
                "reporting_limit_applied": None,
                "spike_value": None,
                "native_value": None,
                "reference_value": None,
                "withheld": True,
                "evaluation": {"status": m["evaluation"]["status"], "measures": measures},
            }
        )
    issues = []
    for issue in evidence["issues"]:
        kind, _, rest = issue["key"].partition(":")
        target = rest.split(":")[0]
        hidden = (kind == "m" and target.isdigit() and int(target) in foreign_measurements) or (
            kind == "x" and target.isdigit() and int(target) in foreign
        )
        if hidden:
            issue = {
                **issue,
                "message": f"{issue['qc_type'].replace('_', ' ').capitalize()} prepared from a "
                f"sample not in this report: {issue['code'].replace('_', ' ')}.",
                "note": "",
                "note_withheld": bool(issue["note"]),
            }
        issues.append(issue)
    return {**evidence, "measurements": measurements, "issues": issues}


def assemble(report, *, actor=None):
    lab = Lab.objects.first()
    # Canonical configured zone (never the Lab.timezone copy). Content stores aware ISO
    # timestamps with offsets plus the zone name, so issued reports never reinterpret them.
    zone = ZoneInfo(settings.TIME_ZONE)
    # The laboratory identity a report issued today prints. Its parts are present only when
    # recorded, so content without a profile keeps the keys (and fingerprint) it always had.
    issued_on = timezone.localdate(timezone=zone)

    def when(dt):
        return None if dt is None else timezone.localtime(dt, zone).isoformat()

    project = report.project
    client = project.client
    samples = _report_samples(report)
    blockers, readiness, revision_ids, batch_ids, requested = [], [], [], set(), {}
    batches, methods, qualifiers, qc_states = {}, {}, {}, {}
    report_blockers = []
    if not samples:
        report_blockers.append("Select at least one sample.")
    if len(samples) > MAX_SAMPLES:
        report_blockers.append(f"A report can hold at most {MAX_SAMPLES} samples.")
    eligibility = {}
    for item in reportable_results(samples=samples):
        eligibility.setdefault(item.requested_test.sample_id, []).append(item)
    external = external_reportable_results(
        [i.requested_test.pk for items in eligibility.values() for i in items]
    )
    external_tests, subcontracted = {}, []
    result_of = {
        rt: (pk, batch)
        for rt, pk, batch in Result.objects.filter(requested_test__sample__in=samples).values_list(
            "requested_test_id", "pk", "batch_id"
        )
    }
    revisions = {
        r.pk: r
        for r in ResultRevision.objects.filter(
            pk__in=[i.revision.pk for items in eligibility.values() for i in items if i.revision]
        ).select_related(
            "batch__method",
            "definition__analyte",
            "definition__method",
            "entered_by",
            "submitted_by",
            "reviewed_by",
        )
    }
    sample_content = []
    for sample in samples:
        state = SampleReadiness(sample)
        if sample.order.project_id != project.pk:
            state.blockers.append(f"{sample.lab_id} belongs to another project.")
        if sample.disposition in UNREPORTABLE_DISPOSITIONS:
            state.blockers.append(
                f"{sample.lab_id} has receipt disposition '{sample.get_disposition_display()}'."
            )
        items = eligibility.get(sample.pk, [])
        if not items:
            state.blockers.append(f"{sample.lab_id} has no requested tests.")
        rows = []
        for item in sorted(
            items,
            key=lambda i: (
                i.requested_test.definition.analyte.name,
                i.requested_test.definition_id,
            ),
        ):
            analyte = item.requested_test.definition.analyte.name
            preservation = item.preservation
            if item.requested_test.pk in external:
                row = _external_row(
                    sample,
                    item.requested_test,
                    external[item.requested_test.pk],
                    actor,
                    when,
                    state,
                    external_tests,
                    subcontracted,
                    preservation,
                )
                if row:
                    rows.append(row)
                continue
            requested[item.requested_test.pk] = item.revision.pk if item.eligible else None
            label = f"{sample.lab_id} {analyte}"
            result_id, batch_id = result_of.get(item.requested_test.pk, (None, None))
            if not item.eligible:
                state.tests.append(
                    ResultReadiness(
                        analyte,
                        item.held[-1].message if item.held else "Held",
                        [f"{label}: {h.message}" for h in item.held]
                        or [f"{label}: no approved result."],
                        result_id,
                        batch_id,
                        preservation_id=item.requested_test.pk,
                    )
                )
                continue
            revision = revisions[item.revision.pk]
            problems = []
            if actor is not None and actor.pk in (revision.entered_by_id, revision.submitted_by_id):
                problems.append(
                    f"{label}: you entered or submitted this result, so you cannot release it."
                )
            state.tests.append(
                ResultReadiness(
                    analyte,
                    revision.get_status_display(),
                    problems,
                    result_id,
                    revision.batch_id,
                    preservation_id=item.requested_test.pk,
                    preparation=preparation_snapshot(revision),
                )
            )
            batch_ids.add(revision.batch_id)
            batch = batches.setdefault(
                revision.batch.number,
                {
                    "number": revision.batch.number,
                    "method": str(revision.batch.method),
                    "matrix": revision.batch.matrix,
                    "qc": {},
                },
            )
            if analyte not in batch["qc"]:
                if revision.batch_id not in qc_states:
                    qc_states[revision.batch_id] = BatchQC.load(revision.batch)
                qc = qc_states[revision.batch_id]
                evidence = qc.evidence(revision.definition)
                evidence.pop("evaluated_at", None)  # keeps the fingerprint deterministic
                batch["qc"][analyte] = {
                    **_scoped_qc(evidence, qc, client.pk),
                    "test_definition_id": revision.definition_id,
                }
            if revision.prepared_aliquot_id:
                prep = revision.prepared_aliquot.batch
                batch_ids.add(prep.pk)
                methods[f"{prep.method.code}|{prep.method.version}"] = {
                    "code": prep.method.code,
                    "version": prep.method.version,
                    "title": prep.method.title,
                    "source": prep.method.source,
                }
                prep_batch = batches.setdefault(
                    prep.number,
                    {
                        "number": prep.number,
                        "kind": "preparation",
                        "method": str(prep.method),
                        "method_code": prep.method.code,
                        "method_version": prep.method.version,
                        "prepared_at": when(prep.prepared_at),
                        "completed_at": when(prep.completed_at),
                        "matrix": prep.matrix,
                        "qc": {},
                    },
                )
                if analyte not in prep_batch["qc"]:
                    if prep.pk not in qc_states:
                        qc_states[prep.pk] = BatchQC.load(prep)
                    prep_qc = qc_states[prep.pk]
                    evidence = prep_qc.evidence(revision.definition)
                    evidence.pop("evaluated_at", None)
                    prep_batch["qc"][analyte] = {
                        **_scoped_qc(evidence, prep_qc, client.pk),
                        "test_definition_id": revision.definition_id,
                    }
            method = revision.definition.method
            methods[f"{revision.method_code}|{revision.method_version}"] = {
                "code": revision.method_code,
                "version": revision.method_version,
                "title": method.title,
                "source": method.source,
            }
            for q in revision.qualifiers:
                qualifiers[q["code"]] = q["description"]
            revision_ids.append(revision.pk)
            row = {
                "preservation": preservation_snapshot(preservation),
                "result_id": revision.result_id,
                # The test this result and its batch QC share (BatchQC.issues(definition));
                # absent from snapshots issued before it was recorded.
                "test_definition_id": revision.definition_id,
                "revision_id": revision.pk,
                "revision": revision.number,
                "analyte": revision.analyte_name,
                "cas_number": revision.definition.analyte.cas_number,
                "method_code": revision.method_code,
                "method_version": revision.method_version,
                "batch": revision.batch.number,
                "kind": revision.kind,
                "reported": "ND" if revision.is_nondetect else revision.supplied_text.strip(),
                "supplied_text": revision.supplied_text,
                "value": number_text(revision.value),
                "unit": revision.unit,
                "reporting_limit": number_text(revision.reporting_limit_applied),
                "method_reporting_limit": number_text(revision.method_reporting_limit),
                "dilution_factor": number_text(revision.dilution_factor),
                "qualifiers": [q["code"] for q in revision.qualifiers],
                "prepared_at": when(revision.prepared_at),
                "analysis_started_at": when(revision.analysis_started_at),
                "analyst": person(revision.entered_by),
                "reviewed_by": person(revision.reviewed_by),
                "reviewed_at": when(revision.reviewed_at),
                "holding_status": revision.get_holding_status_display(),
                "holding": [
                    {
                        "name": h.get("name"),
                        "status": h.get("status"),
                        "deadline": h.get("deadline"),
                        "end_at": h.get("end_at"),
                    }
                    for h in revision.holding_evaluation
                ],
                "correction_reason": revision.correction_reason,
                "comments": revision.comments,
            }
            if revision.prepared_aliquot_id:
                row["preparation"] = preparation_snapshot(revision)
                row["preparation"]["prepared_at"] = when(revision.prepared_at)
                row["preparation"]["completed_at"] = when(
                    revision.prepared_aliquot.batch.completed_at
                )
            basis = revision.basis_snapshot
            if basis["result_basis"] != "unknown":
                # Frozen only when recorded. A legacy "not recorded" revision keeps the key-less
                # row it always had, so an unchanged old result never looks changed in an amendment.
                row.update(basis)
            rows.append(row)
        blockers += state.blockers + [p for t in state.tests for p in t.problems]
        readiness.append(state)
        sample_content.append(
            {
                "lab_id": sample.lab_id,
                "client_sample_id": sample.client_sample_id,
                "coc_reference": sample.order.coc_reference,
                "matrix": sample.get_matrix_display(),
                # Present only when recorded, so content for samples received before these
                # fields existed is unchanged and amendments of older reports show no noise.
                **({"source": sample.get_source_type_display()} if sample.source_type else {}),
                **(
                    {"sample_type": sample.get_sample_type_display()}
                    if sample.sample_type != sample.SampleType.SAMPLE
                    else {}
                ),
                **({"coolant": sample.get_coolant_display()} if sample.coolant else {}),
                "collection_type": sample.get_collection_type_display(),
                "collection_started_at": when(sample.collection_started_at),
                "collected_at": when(sample.collected_at),
                "collection_location": sample.collection_location,
                "collector": sample.collector_name,
                "received_at": when(sample.received_at),
                "received_by": person(sample.received_by),
                "cooler_temperature": sample.cooler_temperature_original
                or number_text(sample.cooler_temperature),
                "cooler_temperature_unit": sample.cooler_temperature_unit or "°C",
                "temperature_instrument": sample.temperature_instrument,
                "seal_condition": sample.seal_condition,
                "preservation_check": sample.preservation_check,
                "discrepancy": sample.discrepancy,
                "disposition": sample.get_disposition_display(),
                "disposition_note": sample.disposition_note,
                "containers": [
                    {"label": c.label, "description": c.description, "condition": c.condition}
                    for c in sample.containers.all()
                ],
                "results": rows,
            }
        )
    if len(revision_ids) + len(external_tests) > MAX_RESULTS:
        report_blockers.append(f"A report can hold at most {MAX_RESULTS} results.")
    blockers = report_blockers + blockers
    content = {
        "schema": SCHEMA,
        "synthetic_notice": synthetic_notice(),
        "lab": {
            "name": lab.name if lab else "",
            "timezone": str(zone),
            **report_identity(lab, on=issued_on),
        },
        "report": {"number": report.number, "title": report.title, "narrative": report.narrative},
        "client": {
            "name": client.name,
            "contact_name": client.contact_name,
            "email": client.email,
            **({"address": client.address_lines} if client.address_lines else {}),
        },
        "project": {"name": project.name, "program": project.program},
        "samples": sample_content,
        "batches": [batches[k] for k in sorted(batches)],
        "methods": [methods[k] for k in sorted(methods)],
        "qualifiers": [{"code": k, "description": qualifiers[k]} for k in sorted(qualifiers)],
    }
    if subcontracted:
        content["subcontracted"] = subcontracted
    return Assembly(
        content,
        fingerprint(content),
        blockers,
        readiness,
        sorted(revision_ids),
        sorted(batch_ids),
        requested,
        report_blockers,
        external_tests,
        issued_on,
    )


def _external_row(
    sample, requested_test, item, actor, when, state, external_tests, subcontracted, preservation
):
    """Frozen row for an actively subcontracted test: the accepted external source result exactly
    as reported, attributed to the external laboratory. No internal batch, QC or analyst claims.
    Returns None (and records blockers) when the source is not reportable."""
    analyte = requested_test.definition.analyte.name
    label = f"{sample.lab_id} {analyte}"
    contract, source = item.subcontract, item.revision
    problems = [f"{label}: {p}" for p in list(item.problems) + preservation_blockers(preservation)]
    if source is not None and actor is not None and actor.pk == source.entered_by_id:
        problems.append(f"{label}: you entered this external result, so you cannot release it.")
    status = "Subcontracted · accepted" if not problems else "Subcontracted"
    state.tests.append(
        ResultReadiness(
            analyte, status, problems, subcontract_id=contract.pk, preservation_id=requested_test.pk
        )
    )
    if item.problems:
        return None
    frozen = source.source_snapshot
    external_tests[requested_test.pk] = source.pk
    subcontracted.append(
        {
            "subcontract": contract.number,
            "laboratory": frozen.get("external_lab", ""),
            "report_reference": source.external_report_reference,
            "lab_id": sample.lab_id,
            "external_sample_id": frozen.get("external_sample_id", ""),
            "analyte": frozen.get("analyte_name", analyte),
            "method": source.external_method,
            "qualifiers": source.qualifier_text,
            "analyst": source.external_analyst,
            "source_sha256": source.source_sha256,
        }
    )
    return {
        "source": "external",
        "preservation": preservation_snapshot(preservation),
        "result_id": None,
        "revision_id": None,
        "external_result_id": source.pk,
        "revision": source.number,
        "analyte": frozen.get("analyte_name", analyte),
        "cas_number": frozen.get("cas_number", ""),
        "method_code": source.external_method,
        "method_version": "",
        "batch": None,
        "kind": source.kind,
        "reported": "ND" if source.kind == "non_detect" else source.observed_text.strip(),
        "supplied_text": source.observed_text,
        "value": number_text(source.value),
        "unit": source.unit,
        "reporting_limit": number_text(source.reporting_limit),
        "method_reporting_limit": None,
        "dilution_factor": number_text(source.dilution),
        "qualifiers": [],
        "external_qualifiers": source.qualifier_text,
        "prepared_at": None,
        "analysis_started_at": when(source.analyzed_at),
        "analyst": None,
        "entered_by": person(source.entered_by),
        "reviewed_by": person(source.reviewed_by),
        "reviewed_at": when(source.reviewed_at),
        "holding_status": "Not evaluated by this laboratory",
        "holding": [],
        "correction_reason": source.reason,
        "comments": "",
        "external": {
            "subcontract": contract.number,
            "laboratory": frozen.get("external_lab", ""),
            "report_reference": source.external_report_reference,
            "external_sample_id": frozen.get("external_sample_id", ""),
            "analyst": source.external_analyst,
            "source_sha256": source.source_sha256,
        },
    }


def _result_line(row):
    qualifiers = f" [{', '.join(row['qualifiers'])}]" if row["qualifiers"] else ""
    return (
        f"{row['reported']} {row['unit']}{qualifiers}, RL {row['reporting_limit']} "
        f"(revision {row['revision']})"
    )


def _preservation_change(before, after):
    """Name changed evidence without interpreting historical absence as a finding."""
    old_label = before.get("label", "Not recorded") if before else "Not recorded"
    new_label = after.get("label", "Not recorded") if after else "Not recorded"
    details = [f"{old_label} → {new_label}"] if old_label != new_label else []
    if before and after:
        old_evidence, new_evidence = before.get("evidence", {}), after.get("evidence", {})
        if old_evidence.get("rule") != new_evidence.get("rule"):
            details.append("rule or source changed")
        if old_evidence.get("observations") != new_evidence.get("observations"):
            details.append("receipt evidence changed")
    old_decision = before.get("decision") if before else None
    new_decision = after.get("decision") if after else None
    if old_decision != new_decision:
        details.append(
            f"lab disposition recorded: {new_decision['label']}"
            if new_decision
            else "no current lab disposition"
        )
    return "; ".join(details) or "assessment evidence changed"


def change_text(change):
    """The sentence of one amendment change. Versions issued before structured changes stored
    preformatted strings; newer ones store {"text", and for a value change "subject", "field",
    "old", "new"}. Issued versions are never rewritten, so both forms stay readable."""
    return change if isinstance(change, str) else change["text"]


# Single-line identity values whose old and new text an amendment can show side by side.
VALUE_FIELDS = {
    ("lab", "name"),
    ("lab", "timezone"),
    ("lab", "phone"),
    ("lab", "email"),
    ("lab", "website"),
    ("report", "title"),
    ("client", "name"),
    ("client", "contact_name"),
    ("client", "email"),
    ("project", "name"),
    ("project", "program"),
}


# Keys compared on their own (preservation, preparation) or not printed at all: the test
# definition reference links a result to its batch QC and is absent from older snapshots, so it
# never makes an amendment look changed.
ROW_EVIDENCE = {"preservation", "preparation", "test_definition_id"}


def _without_references(batches):
    return [
        {
            **b,
            "qc": {
                analyte: {k: v for k, v in qc.items() if k != "test_definition_id"}
                for analyte, qc in b["qc"].items()
            },
        }
        for b in batches
    ]


def describe_changes(old, new):
    """Differences between two contents, for amendment records: one entry per change, each with
    its sentence ("text") and, where a value was replaced, its "subject", "field", "old" and
    "new" text so a report can print the old value struck through."""
    changes = []

    def add(text, **value):
        changes.append({"text": text, **value})

    labels = {
        ("lab", "name"): "Laboratory name",
        ("lab", "timezone"): "Laboratory time zone",
        # Identity parts are absent from snapshots that did not record them; absent on both
        # sides is no change, absent on one side is.
        ("lab", "address"): "Laboratory address",
        ("lab", "phone"): "Laboratory phone",
        ("lab", "email"): "Laboratory email",
        ("lab", "website"): "Laboratory website",
        ("lab", "logo"): "Laboratory logo",
        ("lab", "certificates"): "Laboratory certificates",
        ("lab", "certificate_note"): "Laboratory certificate statement",
        ("report", "title"): "Report title",
        ("report", "narrative"): "Case narrative",
        ("client", "name"): "Client name",
        ("client", "contact_name"): "Client contact",
        ("client", "email"): "Client email",
        ("client", "address"): "Client address",
        ("project", "name"): "Project name",
        ("project", "program"): "Program / permit",
    }
    for (section, key), label in labels.items():
        before, after = old.get(section, {}).get(key), new.get(section, {}).get(key)
        if before == after:
            continue
        if (section, key) in VALUE_FIELDS and before and after:
            add(f"{label} changed.", subject=label, field=f"{section}.{key}", old=before, new=after)
        else:
            add(f"{label} changed.")
    old_samples = {s["lab_id"]: s for s in old["samples"]}
    new_samples = {s["lab_id"]: s for s in new["samples"]}
    for lab_id in sorted(old_samples.keys() - new_samples.keys()):
        add(f"Sample {lab_id} removed.")
    for lab_id in sorted(new_samples.keys() - old_samples.keys()):
        add(f"Sample {lab_id} added.")
    for lab_id in sorted(old_samples.keys() & new_samples.keys()):
        before, after = old_samples[lab_id], new_samples[lab_id]
        # A field absent from an older snapshot (added to the schema later) counts as blank.
        fields = sorted(
            k for k in after if k != "results" and (before.get(k) or None) != (after[k] or None)
        )
        if fields:
            add(f"Sample {lab_id}: {', '.join(f.replace('_', ' ') for f in fields)} changed.")
        old_rows = {(r["analyte"], r["method_code"]): r for r in before["results"]}
        new_rows = {(r["analyte"], r["method_code"]): r for r in after["results"]}
        for key in sorted(old_rows.keys() | new_rows.keys()):
            a, b = old_rows.get(key), new_rows.get(key)
            if a is None:
                add(f"{lab_id} {key[0]}: added, {_result_line(b)}.")
            elif b is None:
                add(f"{lab_id} {key[0]}: removed.")
            elif a != b:
                if a.get("preservation") != b.get("preservation"):
                    details = _preservation_change(a.get("preservation"), b.get("preservation"))
                    if not a.get("preservation"):
                        add(
                            f"{lab_id} {key[0]}: preservation assessment added "
                            f"(not recorded in the prior version; {details})."
                        )
                    else:
                        add(f"{lab_id} {key[0]}: preservation assessment changed ({details}).")
                if a.get("preparation") != b.get("preparation"):
                    change = (
                        "added (not previously recorded)" if not a.get("preparation") else "changed"
                    )
                    add(f"{lab_id} {key[0]}: independent preparation evidence {change}.")
                if _result_line(a) != _result_line(b):
                    add(
                        f"{lab_id} {key[0]}: {_result_line(a)} → {_result_line(b)}.",
                        subject=f"{lab_id} {key[0]}",
                        field="result",
                        old=_result_line(a),
                        new=_result_line(b),
                    )
                elif {k: v for k, v in a.items() if k not in ROW_EVIDENCE} != {
                    k: v for k, v in b.items() if k not in ROW_EVIDENCE
                }:
                    add(f"{lab_id} {key[0]}: result details changed (revision {b['revision']}).")
    if _without_references(old["batches"]) != _without_references(new["batches"]):
        add("Batch QC summary changed.")
    if old["methods"] != new["methods"] or old["qualifiers"] != new["qualifiers"]:
        add("Method or qualifier descriptions changed.")
    return changes
