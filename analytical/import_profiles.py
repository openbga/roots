"""Mapped tabular result import: saved column mappings and their conversion to import rows.

Instrument exports are configurable, so nothing here claims to read a particular vendor's
files. An operator describes a file with a profile: the layout (long or wide), the delimiter
and which column holds what. The profile never infers an analyte, unit, non-detect or
acceptance from a header. A file is converted into the same canonical rows the strict CSV
import validates, and the existing validation and entry services do the rest.

Two layouts are supported, both comma or tab delimited UTF-8 text:

* long: one result per row, with columns for sample, analyte and result.
* wide: one sample per row, with one column for each analyte the operator maps.

Result text is carried exactly as supplied. Nothing is converted to a binary float here.
"""

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.roles import require_role
from catalog.models import Analyte
from core.audit import record_event
from core.concurrency import check_original_state

from .models import ImportProfile

LONG, WIDE = ImportProfile.Layout.LONG, ImportProfile.Layout.WIDE
DELIMITERS = {"comma": ",", "tab": "\t"}
MAX_HEADER_ROW = 50
SEPARATOR = "|"

DATE_FORMATS = {
    "iso": "ISO 8601 (2026-11-08T14:30)",
    "us_24h": "MM/DD/YYYY HH:MM (24-hour)",
    "us_12h": "MM/DD/YYYY HH:MM AM/PM",
}
_US_PATTERNS = {
    "us_24h": ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"),
    "us_12h": ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p"),
}

# Roles that take one column or an explicitly supplied fixed value.
COLUMN_ROLES = ("sample", "analysis_started_at")
OPTIONAL_COLUMN_ROLES = ("qualifiers", "comments", "reason", "prepared_aliquot_id")
VALUE_ROLES = (
    "unit",
    "reporting_limit",
    "dilution_factor",
    "result_basis",
    "percent_solids",
    "solids_reference",
)
ROLE_LABELS = {
    "sample": "Sample ID",
    "analysis_started_at": "Analysis start",
    "analyte": "Analyte",
    "result": "Result",
    "unit": "Unit",
    "reporting_limit": "Reporting limit",
    "dilution_factor": "Dilution factor",
    "qualifiers": "Qualifiers",
    "comments": "Comments",
    "reason": "Reason for correction",
    "prepared_aliquot_id": "Prepared aliquot ID",
    "result_basis": "Result basis",
    "percent_solids": "Percent solids",
    "solids_reference": "Solids source reference",
}


# ------------------------------------------------------------------------------- mapping


def _source(spec):
    """(kind, text) for a value role: a column header or a fixed value, else (None, '')."""
    if isinstance(spec, dict):
        for kind in ("column", "fixed"):
            text = spec.get(kind)
            if isinstance(text, str) and text.strip():
                return kind, text.strip()
    return None, ""


def used_columns(layout, mapping):
    """Every header the mapping reads, with a label for messages, in a stable order."""
    used = [(mapping.get(role), ROLE_LABELS[role]) for role in COLUMN_ROLES]
    if layout == LONG:
        used += [(mapping.get("analyte"), "Analyte"), (mapping.get("result"), "Result")]
    for role in VALUE_ROLES:
        kind, text = _source(mapping.get(role))
        if kind == "column":
            used.append((text, ROLE_LABELS[role]))
    used += [(mapping.get(role), ROLE_LABELS[role]) for role in OPTIONAL_COLUMN_ROLES]
    if layout == WIDE:
        used += [(c.get("column"), "Analyte column") for c in mapping.get("analyte_columns", [])]
    return [(header, label) for header, label in used if header]


def validate_mapping(layout, mapping):
    """Problems with a mapping as a list of messages (empty when it is usable)."""
    problems = []
    if layout not in (LONG, WIDE):
        return ["Choose a layout."]
    header_row = mapping.get("header_row")
    if not isinstance(header_row, int) or not 1 <= header_row <= MAX_HEADER_ROW:
        problems.append(f"The header row must be a number from 1 to {MAX_HEADER_ROW}.")
    if mapping.get("date_format") not in DATE_FORMATS:
        problems.append("Choose the date format used by the file.")
    for role in COLUMN_ROLES:
        if not mapping.get(role):
            problems.append(f"Name the column that holds the {ROLE_LABELS[role].lower()}.")
    if layout == LONG:
        for role in ("analyte", "result"):
            if not mapping.get(role):
                problems.append(f"Name the column that holds the {ROLE_LABELS[role].lower()}.")
        if not _source(mapping.get("unit"))[0]:
            problems.append(
                "Say where the unit comes from: a column in the file, or a fixed unit the file "
                "uses. The unit is never assumed."
            )
    for role in ("reporting_limit", "dilution_factor"):
        if not _source(mapping.get(role))[0] and not (
            role == "reporting_limit"
            and layout == WIDE
            and mapping.get("analyte_columns")
            and all(c.get("reporting_limit") for c in mapping["analyte_columns"])
        ):
            problems.append(
                f"Say where the {ROLE_LABELS[role].lower()} comes from: a column or a fixed value."
            )
    if layout == WIDE:
        columns = mapping.get("analyte_columns") or []
        if not columns:
            problems.append("Map at least one analyte column.")
        for entry in columns:
            for key, label in (
                ("column", "column header"),
                ("analyte", "analyte"),
                ("unit", "unit"),
            ):
                if not (entry.get(key) or "").strip():
                    problems.append(
                        f"Every analyte column needs a {label} "
                        f"(check '{entry.get('column') or entry.get('analyte') or '?'}')."
                    )
        analytes = [(c.get("analyte") or "").casefold() for c in columns]
        for name in sorted({a for a in analytes if analytes.count(a) > 1}):
            problems.append(f"The analyte '{name}' is mapped to more than one column.")
    headers = [h for h, _ in used_columns(layout, mapping)]
    for header in sorted({h for h in headers if headers.count(h) > 1}):
        roles = ", ".join(
            sorted({label for h, label in used_columns(layout, mapping) if h == header})
        )
        problems.append(f"The column '{header}' is used more than once ({roles}).")
    return problems


def snapshot(profile):
    """What an import records about the mapping it used."""
    return {
        "profile_id": profile.pk,
        "name": profile.name,
        "layout": profile.layout,
        "delimiter": profile.delimiter,
        "mapping": profile.mapping,
    }


def mapping_hash(profile):
    """SHA-256 of the meaning of the profile (layout, delimiter, mapping), not of its name."""
    meaning = {"layout": profile.layout, "delimiter": profile.delimiter, "mapping": profile.mapping}
    blob = json.dumps(meaning, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------ saving profiles


def _check(name, layout, delimiter, mapping, *, exclude_pk=None):
    errors = {}
    name = (name or "").strip()
    if not name:
        errors["name"] = "Give the profile a name."
    elif ImportProfile.objects.filter(name__iexact=name).exclude(pk=exclude_pk).exists():
        errors["name"] = "A profile with this name already exists."
    if delimiter not in DELIMITERS:
        errors["delimiter"] = "Choose comma or tab."
    problems = validate_mapping(layout, mapping)
    if layout == WIDE:
        known = {a.casefold() for a in Analyte.objects.values_list("name", flat=True)}
        for entry in mapping.get("analyte_columns", []):
            if (entry.get("analyte") or "").casefold() not in known:
                problems.append(f"'{entry.get('analyte')}' is not an analyte in the catalog.")
    for alias, analyte in (mapping.get("analyte_aliases") or {}).items():
        if layout == LONG and analyte.casefold() not in {
            a.casefold() for a in Analyte.objects.values_list("name", flat=True)
        }:
            problems.append(f"'{analyte}' (for '{alias}') is not an analyte in the catalog.")
    if problems:
        errors["__all__"] = problems
    if errors:
        raise ValidationError(errors)
    return name


@transaction.atomic
def create_profile(*, actor, name, layout, delimiter, mapping, notes=""):
    from .services import ANALYST

    require_role(actor, ANALYST)
    name = _check(name, layout, delimiter, mapping)
    profile = ImportProfile.objects.create(
        name=name,
        layout=layout,
        delimiter=delimiter,
        mapping=mapping,
        notes=(notes or "").strip(),
        created_by=actor,
    )
    record_event(
        actor=actor,
        action="import_profile.created",
        obj=profile,
        after=snapshot(profile) | {"mapping_hash": mapping_hash(profile)},
    )
    return profile


@transaction.atomic
def update_profile(
    *,
    actor,
    profile_id,
    name,
    layout,
    delimiter,
    mapping,
    notes="",
    is_active=True,
    expected_state=None,
):
    from .services import ANALYST

    require_role(actor, ANALYST)
    profile = ImportProfile.objects.select_for_update().get(pk=profile_id)
    check_original_state(profile, expected_state)
    name = _check(name, layout, delimiter, mapping, exclude_pk=profile.pk)
    before = snapshot(profile) | {
        "mapping_hash": mapping_hash(profile),
        "active": profile.is_active,
    }
    profile.name, profile.layout, profile.delimiter = name, layout, delimiter
    profile.mapping, profile.notes = mapping, (notes or "").strip()
    profile.is_active, profile.updated_by = is_active, actor
    profile.save()
    record_event(
        actor=actor,
        action="import_profile.updated",
        obj=profile,
        before=before,
        after=snapshot(profile) | {"mapping_hash": mapping_hash(profile), "active": is_active},
    )
    return profile


# ----------------------------------------------------------------------------- reading


@dataclass
class ExtractedRow:
    line: int  # record number in the file
    values: dict  # canonical keys the strict import validates
    detail: str  # short provenance kept on the result revision
    source: str  # human description for the preview
    errors: list = field(default_factory=list)


@dataclass
class Extracted:
    rows: list = field(default_factory=list)
    blanks: list = field(default_factory=list)  # (line, sample, column) cells left empty
    unused: list = field(default_factory=list)  # headers present but not mapped
    preamble: int = 0
    file_errors: list = field(default_factory=list)


def normalize_date(text, date_format):
    """Text in the file's declared format as an ISO string for the shared timestamp parser.
    A value without an offset is still read in the configured laboratory zone afterwards."""
    text = (text or "").strip()
    if date_format == "iso":
        return text
    for pattern in _US_PATTERNS[date_format]:
        try:
            return datetime.strptime(text, pattern).isoformat()
        except ValueError:
            continue
    raise ValidationError(
        f"'{text}' does not match the declared date format ({DATE_FORMATS[date_format]})."
    )


def extract(profile, text, *, max_rows):
    """Convert delimited text to canonical rows according to the profile. Reads no database."""
    mapping, out = profile.mapping, Extracted()
    reader = csv.reader(
        io.StringIO(text, newline=""), delimiter=DELIMITERS[profile.delimiter], strict=True
    )
    header = None
    try:
        for number, record in enumerate(reader, start=1):
            if number < mapping["header_row"]:
                out.preamble += 1
                continue
            header, header_number = [cell.strip() for cell in record], number
            break
        if header is None:
            out.file_errors.append(
                f"The file ends before header row {mapping['header_row']}; it has no data."
            )
            return out
        position = {}
        for index, name in enumerate(header):
            position.setdefault(name, []).append(index)
        wanted = used_columns(profile.layout, mapping)
        missing = [f"'{h}' ({label})" for h, label in wanted if h not in position]
        if missing:
            out.file_errors.append(
                f"Header row {mapping['header_row']} has no column named {', '.join(missing)}. "
                "Names are matched exactly; edit the profile or fix the file."
            )
        repeated = sorted({h for h, _ in wanted if len(position.get(h, [])) > 1})
        if repeated:
            out.file_errors.append(
                f"The column name(s) {', '.join(repr(h) for h in repeated)} appear more than once "
                "in the header; a mapped column must be unique."
            )
        if out.file_errors:
            return out
        mapped = {h for h, _ in wanted}
        out.unused = [h for h in header if h not in mapped]
        for line, record in enumerate(reader, start=header_number + 1):
            if not any(cell.strip() for cell in record):
                continue
            if len(out.rows) + len(out.blanks) >= max_rows:
                out.file_errors.append(f"More than {max_rows} rows; split the file.")
                break
            _convert(profile, line, record, header, position, out)
    except csv.Error as exc:
        out.file_errors.append(
            f"Malformed {profile.get_delimiter_display().lower()}-delimited text near row "
            f"{reader.line_num}: {exc}"
        )
    return out


def _convert(profile, number, record, header, position, out):
    m, fmt = profile.mapping, profile.mapping["date_format"]
    where = profile.layout
    if len(record) != len(header):
        out.rows.append(
            ExtractedRow(
                number,
                {"sample_id": record[0] if record else ""},
                f"row {number}",
                f"row {number}",
                [f"Expected {len(header)} columns, found {len(record)}."],
            )
        )
        return

    def cell(name):
        return record[position[name][0]]

    def value(role):
        kind, text = _source(m.get(role))
        return cell(text) if kind == "column" else text

    errors = []
    started = cell(m["analysis_started_at"])
    try:
        started = normalize_date(started, fmt)
    except ValidationError as exc:
        errors.extend(exc.messages)
    common = {
        "result_basis": value("result_basis"),
        "percent_solids": value("percent_solids"),
        "solids_reference": value("solids_reference"),
        "sample_id": cell(m["sample"]).strip(),
        "analysis_started_at": started,
        "dilution_factor": value("dilution_factor"),
        "qualifiers": cell(m["qualifiers"]) if m.get("qualifiers") else "",
        "comments": cell(m["comments"]) if m.get("comments") else "",
        "reason": cell(m["reason"]) if m.get("reason") else "",
        "prepared_aliquot_id": cell(m["prepared_aliquot_id"])
        if m.get("prepared_aliquot_id")
        else "",
    }
    if where == LONG:
        label = cell(m["analyte"]).strip()
        aliases = {k.casefold(): v for k, v in (m.get("analyte_aliases") or {}).items()}
        unit = value("unit")
        if not unit.strip():
            errors.append("The unit cell is blank. Units are never assumed.")
        out.rows.append(
            ExtractedRow(
                number,
                common
                | {
                    "analyte": aliases.get(label.casefold(), label),
                    "analyte_supplied": label,
                    "result": cell(m["result"]),
                    "reporting_limit": value("reporting_limit"),
                    "unit": unit,
                },
                f"row {number}",
                f"row {number}",
                errors,
            )
        )
        return
    for entry in m["analyte_columns"]:
        text = cell(entry["column"])
        if not text.strip():
            out.blanks.append((number, common["sample_id"], entry["column"]))
            continue
        limit = entry.get("reporting_limit") or value("reporting_limit")
        out.rows.append(
            ExtractedRow(
                number,
                common
                | {
                    "analyte": entry["analyte"],
                    "analyte_supplied": entry["column"],
                    "result": text,
                    "reporting_limit": limit,
                    "unit": entry["unit"],
                },
                f"row {number}, column {entry['column'][:40]}",
                f"row {number}, column '{entry['column']}'",
                list(errors),
            )
        )
