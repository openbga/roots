"""Bounded CSV result import with full validation before anything is written.

One file imports into one open batch, all or nothing. Rows are matched to results already
assigned to the batch; nothing is created implicitly and no value is coerced.
"""

import base64
import codecs
import csv
import hashlib
import io
import json
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import require_role
from core.models import AuditEvent

from . import import_profiles, services
from .models import Batch, ImportProfile, Result, ResultRevision
from .parsing import parse_timestamp
from .preparation import lock_work_batches

MAX_BYTES = 1024 * 1024
MAX_ROWS = 2000
REQUIRED = (
    "sample_id",
    "analyte",
    "result",
    "reporting_limit",
    "dilution_factor",
    "analysis_started_at",
)
OPTIONAL = (
    "unit",
    "qualifiers",
    "comments",
    "reason",
    "result_basis",
    "percent_solids",
    "solids_reference",
    "prepared_aliquot_id",
)
COLUMNS = REQUIRED + OPTIONAL


@dataclass
class ImportRow:
    line: int
    values: dict
    result: Result | None = None
    entry: dict | None = None
    errors: list = field(default_factory=list)
    detail: str | None = None  # provenance kept on the revision; the strict import derives it
    source: str = ""  # where in the file this came from, for the preview
    previous: ResultRevision | None = None  # the latest revision this would follow

    @property
    def ok(self):
        return not self.errors


@dataclass
class ImportPreview:
    filename: str
    rows: list = field(default_factory=list)
    file_errors: list = field(default_factory=list)
    # Only set by the mapped import.
    profile: object = None
    mapping_hash: str = ""
    file_hash: str = ""
    state_token: str = ""  # fingerprint of the results this would follow, to detect stale previews
    blanks: list = field(default_factory=list)
    unused_columns: list = field(default_factory=list)
    preamble_rows: int = 0

    @property
    def ok(self):
        return not self.file_errors and bool(self.rows) and all(r.ok for r in self.rows)

    @property
    def error_count(self):
        return len(self.file_errors) + sum(len(r.errors) for r in self.rows)


def encode_payload(data):
    """Uploaded bytes as ASCII for a hidden form field. The browser cannot alter it: base64 has
    no whitespace, line endings or trimming to normalise."""
    return base64.b64encode(data).decode("ascii")


def file_digest(data):
    """SHA-256 of the uploaded bytes exactly as received (byte order mark and line endings kept)."""
    return hashlib.sha256(data).hexdigest()


def decode_upload(data):
    """Decode uploaded bytes as UTF-8 (a BOM is allowed). Raises ValidationError."""
    if len(data) > MAX_BYTES:
        raise ValidationError(f"The file is larger than {MAX_BYTES // 1024} KiB.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("The file is not UTF-8 text. Export it as CSV (UTF-8).") from None


def _flatten(exc):
    if hasattr(exc, "error_dict"):
        return [f"{name}: {msg}" for name, errs in exc.message_dict.items() for msg in errs]
    return list(exc.messages)


def _lookup(batch):
    """The batch's results by (lab sample ID, analyte name), lower-cased."""
    lookup = {}
    results = Result.objects.filter(batch=batch).select_related(
        "requested_test__sample",
        "requested_test__definition__analyte",
        "requested_test__definition__method",
    )
    for r in results:
        key = (
            r.requested_test.sample.lab_id.casefold(),
            r.requested_test.definition.analyte.name.casefold(),
        )
        lookup[key] = r
    return lookup


def parse_import(batch, text, *, filename="upload.csv"):
    """Validate CSV text against a batch. Reads the database; writes nothing."""
    preview = ImportPreview(filename=filename)
    if "\x00" in text:
        preview.file_errors.append("The file contains NUL characters; it is not a text CSV.")
        return preview
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
    except csv.Error as exc:
        preview.file_errors.append(f"Could not read the header: {exc}")
        return preview
    if header is None:
        preview.file_errors.append("The file is empty.")
        return preview
    header = [h.strip().lower() for h in header]
    unknown = [h for h in header if h not in COLUMNS]
    missing = [c for c in REQUIRED if c not in header]
    dupes = sorted({h for h in header if header.count(h) > 1})
    if unknown:
        preview.file_errors.append(
            f"Unknown column(s): {', '.join(unknown)}. Allowed: {', '.join(COLUMNS)}."
        )
    if missing:
        preview.file_errors.append(f"Missing required column(s): {', '.join(missing)}.")
    if dupes:
        preview.file_errors.append(f"Duplicate column(s): {', '.join(dupes)}.")
    if preview.file_errors:
        return preview

    lookup = _lookup(batch)
    tz, now, seen = services.lab_timezone(), timezone.now(), {}
    try:
        for number, raw in enumerate(reader, start=2):
            if not any(cell.strip() for cell in raw):
                continue
            if len(preview.rows) >= MAX_ROWS:
                preview.file_errors.append(f"More than {MAX_ROWS} rows; split the file.")
                break
            row = ImportRow(number, {})
            if len(raw) != len(header):
                row.errors.append(f"Expected {len(header)} columns, found {len(raw)}.")
                preview.rows.append(row)
                continue
            row.values = dict(zip(header, raw, strict=True))
            _validate_row(row, lookup, seen, batch, tz, now)
            preview.rows.append(row)
    except csv.Error as exc:
        preview.file_errors.append(f"Malformed CSV near row {reader.line_num}: {exc}")
    if not preview.rows and not preview.file_errors:
        preview.file_errors.append("The file has a header but no data rows.")
    return preview


def _validate_row(row, lookup, seen, batch, tz, now):
    v = row.values
    sample_id, analyte = v["sample_id"].strip(), v["analyte"].strip()
    result = lookup.get((sample_id.casefold(), analyte.casefold()))
    if result is None:
        row.errors.append(
            f"No test '{analyte}' for sample '{sample_id}' is assigned to "
            f"{batch.number}. Add it to the batch first."
        )
        return
    row.result = result
    if result.pk in seen:
        row.errors.append(
            f"Duplicate of row {seen[result.pk]}: one result per sample and analyte per file."
        )
        return
    seen[result.pk] = row.line
    latest = services.latest_revision(result)
    row.previous = latest
    reason = v.get("reason", "").strip()
    if latest and latest.status == "submitted":
        row.errors.append("A revision is awaiting review; it must be returned first.")
    elif latest and latest.status in ("approved", "returned") and not reason:
        row.errors.append(
            "A result already exists. Give a 'reason' for the correction or reanalysis."
        )
    try:
        started = parse_timestamp(v["analysis_started_at"], tz, "analysis_started_at")
    except ValidationError as exc:
        row.errors.extend(exc.messages)
        started = None
    codes = [c for c in v.get("qualifiers", "").replace(",", ";").split(";") if c.strip()]
    rt = result.requested_test
    try:
        row.entry = services.clean_entry(
            definition=rt.definition,
            sample=rt.sample,
            batch=batch,
            supplied_text=v["result"],
            reporting_limit_applied=v["reporting_limit"],
            dilution_factor=v["dilution_factor"],
            analysis_started_at=started,
            qualifier_codes=codes,
            unit=v.get("unit", "").strip() or None,
            result_basis=v.get("result_basis", ""),
            percent_solids=v.get("percent_solids"),
            solids_reference=v.get("solids_reference", ""),
            prepared_aliquot_id=v.get("prepared_aliquot_id"),
            now=now,
        )
        row.entry["qualifier_codes"] = [c.strip() for c in codes]
    except ValidationError as exc:
        # analysis_started_at errors already reported by the parser when started is None.
        row.errors.extend(
            m
            for m in _flatten(exc)
            if not (started is None and m.startswith("analysis_started_at"))
        )


def _write_rows(*, actor, batch, preview, filename, audit_extra=None):
    """Write every previewed row inside the caller's transaction, then record one audit event."""
    # Lock every affected Result in pk order first (the release recheck's order), then
    # enter rows in file order.
    list(
        Result.objects.select_for_update(of=("self",))
        .filter(pk__in=[row.result.pk for row in preview.rows])
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    revisions = []
    for row in preview.rows:
        result = services.lock_result(row.result.pk)
        if result.batch_id != batch.pk:
            raise ValidationError("A batch assignment changed during import. Retry.")
        revisions.append(
            services.enter_result_locked(
                actor=actor,
                result=result,
                supplied_text=row.values["result"],
                reporting_limit_applied=row.values["reporting_limit"],
                dilution_factor=row.values["dilution_factor"],
                analysis_started_at=row.entry["analysis_started_at"],
                qualifier_codes=row.entry["qualifier_codes"],
                comments=row.values.get("comments", ""),
                reason=row.values.get("reason", ""),
                unit=row.values.get("unit", "").strip() or None,
                result_basis=row.values.get("result_basis", ""),
                percent_solids=row.values.get("percent_solids"),
                solids_reference=row.values.get("solids_reference", ""),
                prepared_aliquot_id=row.entry.get("prepared_aliquot_id"),
                source=ResultRevision.Source.IMPORT,
                source_detail=row.detail or f"{filename[:150]}:row {row.line}",
            )
        )
    from core.audit import record_event

    record_event(
        actor=actor,
        action="batch.results_imported",
        obj=batch,
        after={
            "filename": filename,
            "rows": len(revisions),
            "revision_ids": [r.pk for r in revisions],
        }
        | (audit_extra or {}),
    )
    return revisions


def commit_import(*, actor, batch_id, text=None, filename="upload.csv", data=None):
    """Revalidate the file and write every row, or nothing. Returns the revisions.

    Pass the uploaded bytes as `data` so the audit event records their SHA-256 (`text` is then
    read from them); `text` alone is for callers without an original file."""
    require_role(actor, services.ANALYST)
    source = {}
    if data is not None:
        text = decode_upload(data)
        source = {"file_sha256": file_digest(data), "file_bytes": len(data)}
    with transaction.atomic():
        initial = parse_import(Batch.objects.get(pk=batch_id), text, filename=filename)
        if not initial.ok:
            raise ValidationError(
                f"The file has {initial.error_count} error(s). Nothing was imported."
            )
        batch = lock_work_batches(
            [batch_id],
            aliquot_ids={
                r.entry["prepared_aliquot_id"]
                for r in initial.rows
                if r.entry.get("prepared_aliquot_id")
            },
        )[batch_id]
        preview = parse_import(batch, text, filename=filename)
        if not preview.ok:
            raise ValidationError(
                f"The file has {preview.error_count} error(s). Nothing was imported."
            )
        return _write_rows(
            actor=actor, batch=batch, preview=preview, filename=filename, audit_extra=source
        )


# ------------------------------------------------------------------ mapped (profile) import


def parse_profile_import(batch, profile, data, *, filename="upload.txt"):
    """Validate an uploaded file (bytes) against a batch through a saved mapping. Writes nothing.

    The digest is of the uploaded bytes. The text read from them is UTF-8 with a leading byte
    order mark ignored; line endings are read as uploaded."""
    preview = ImportPreview(
        filename=filename,
        profile=profile,
        mapping_hash=import_profiles.mapping_hash(profile),
        file_hash=file_digest(data),
    )
    try:
        text = decode_upload(data)
    except ValidationError as exc:
        preview.file_errors.extend(exc.messages)
        return preview
    if "\x00" in text:
        preview.file_errors.append("The file contains NUL characters; it is not delimited text.")
        return preview
    extracted = import_profiles.extract(profile, text, max_rows=MAX_ROWS)
    preview.file_errors.extend(extracted.file_errors)
    preview.blanks, preview.unused_columns = extracted.blanks, extracted.unused
    preview.preamble_rows = extracted.preamble
    if preview.file_errors:
        return preview
    earlier = _earlier_import(batch, preview.file_hash, preview.mapping_hash)
    if earlier is not None:
        preview.file_errors.append(
            "This file, with this mapping, was already imported into this batch by "
            f"{earlier.actor.get_username()} on "
            f"{timezone.localtime(earlier.occurred_at):%Y-%m-%d %H:%M}. Importing it again would "
            "only add identical revisions. Change the file, or correct results by hand."
        )
    lookup = _lookup(batch)
    tz, now, seen = services.lab_timezone(), timezone.now(), {}
    for item in extracted.rows:
        row = ImportRow(
            item.line,
            item.values,
            errors=list(item.errors),
            detail=f"{filename[:100]}:{item.detail}"[:200],
            source=item.source,
        )
        if not row.errors:
            _validate_row(row, lookup, seen, batch, tz, now)
        preview.rows.append(row)
    state = sorted(
        f"{row.result.pk}:{row.previous.pk if row.previous else 0}:"
        + json.dumps(
            {k: row.entry.get(k) for k in ("prepared_aliquot_id", "prepared_at")}
            if row.entry
            else {},
            sort_keys=True,
            default=str,
        )
        for row in preview.rows
        if row.result is not None
    )
    preview.state_token = hashlib.sha256("|".join(state).encode()).hexdigest()
    if not preview.rows and not preview.file_errors:
        preview.file_errors.append(
            "The file has a header but no values to import."
            + (" Every mapped cell was blank." if preview.blanks else "")
        )
    return preview


def _earlier_import(batch, file_hash, mapping_hash):
    return (
        AuditEvent.objects.filter(
            action="batch.results_imported",
            object_type=batch._meta.label_lower,
            object_id=str(batch.pk),
            after__file_sha256=file_hash,
            after__mapping_sha256=mapping_hash,
        )
        .select_related("actor")
        .order_by("-occurred_at")
        .first()
    )


def commit_profile_import(
    *, actor, batch_id, profile_id, data, filename, mapping_hash, state_token
):
    """Revalidate through the saved mapping and write every row, or nothing.

    `mapping_hash` and `state_token` come from the preview. If the profile was edited, or any
    targeted result changed since, the import is refused so that what is written is what was
    previewed. The same file through the same mapping is refused once imported into a batch, so a
    repeated submit cannot stack identical revisions."""
    require_role(actor, services.ANALYST)
    with transaction.atomic():
        initial = parse_profile_import(
            Batch.objects.get(pk=batch_id),
            ImportProfile.objects.get(pk=profile_id),
            data,
            filename=filename,
        )
        if not initial.ok:
            raise ValidationError(
                f"The file has {initial.error_count} error(s). Nothing was imported."
            )
        batch = lock_work_batches(
            [batch_id],
            aliquot_ids={
                r.entry["prepared_aliquot_id"]
                for r in initial.rows
                if r.entry.get("prepared_aliquot_id")
            },
        )[batch_id]
        profile = ImportProfile.objects.select_for_update().get(pk=profile_id)
        if not profile.is_active:
            raise ValidationError("This mapping profile is no longer active.")
        if import_profiles.mapping_hash(profile) != mapping_hash:
            raise ValidationError(
                "The mapping profile changed after the preview. Preview the file again."
            )
        preview = parse_profile_import(batch, profile, data, filename=filename)
        if not preview.ok:
            raise ValidationError(
                f"The file has {preview.error_count} error(s). Nothing was imported."
            )
        if preview.state_token != state_token:
            raise ValidationError(
                "A result in this batch changed after the preview. Preview the file again."
            )
        return _write_rows(
            actor=actor,
            batch=batch,
            preview=preview,
            filename=filename,
            audit_extra={
                "import_kind": "mapped",
                "file_sha256": preview.file_hash,
                "file_bytes": len(data),
                "file_bom_removed": data.startswith(codecs.BOM_UTF8),
                "mapping_sha256": preview.mapping_hash,
                "mapping_snapshot": import_profiles.snapshot(profile),
                "blank_cells_not_imported": len(preview.blanks),
                "unmapped_columns": preview.unused_columns,
            },
        )
