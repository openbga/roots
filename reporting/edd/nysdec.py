"""Exploratory NYSDEC EDD v5 chemistry export (Sample_v5, TestResultQC_v5, Batch_v5).

Builds EQuIS EDP *input* tables from a released report version's frozen snapshot plus explicit
recipient mappings. It is not a NYSDEC submission: NYSDEC accepts only packages produced by EQuIS
EDP "Sign and Submit" with the data provider's certificate (NYSDEC EDD Manual, May 2024, 5.5.1).
Nothing here guesses a recipient code: every reference-table value comes from the mapping, and a
missing or unrepresentable value is reported as an error that names the record and field to fix.

Field rules cited from the manual: non-detects 4.4.2.4 (result_value null, reporting detection limit
and unit required, U qualifier); re-tests 4.4.2.1; depth requirement Appendix A Sample_v5 K-M;
null fields 5.3.1. Field order, types, lengths and enumerations come from `nysdec_format`.
"""

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .nysdec_format import ENUMERATIONS, FORMAT_NAME, FORMAT_VERSION, REFERENCE_VALUES, SECTIONS

DEPTH_MATRICES = {"SO", "SS", "SE", "WG"}  # manual Appendix A, Sample_v5 columns K-M
SECTION_ORDER = ("Sample_v5", "TestResultQC_v5", "Batch_v5")
PROJECT_FIELDS = {
    "data_provider": "Data provider code (rt_company)",
    "sampling_company_code": "Sampling company code (rt_company)",
    "lab_name_code": "Laboratory code (rt_company)",
    "task_code": "Task code",
}
TEST_FIELDS = {
    "lab_anl_method_name": "NYSDEC analytical method (rt_analytic_method)",
    "fraction": "Fraction (rt_fraction)",
    "preservative": "Preservative (rt_preservative)",
    "basis": "Basis (WET, DRY or NA)",
    "chemical_name": "NYSDEC chemical name (rt_analyte)",
    "unit_code": "NYSDEC unit (rt_unit)",
}
NOTICE = (
    f"Exploratory {FORMAT_NAME} EDD v5 input (format {FORMAT_VERSION}, reference values "
    f"{REFERENCE_VALUES}). Not validated by EQuIS EDP; reference-table codes are not "
    "checked in this application. Load it into EDP, resolve every EDP error, then sign and "
    "submit from EDP."
)


@dataclass(frozen=True)
class Mapping:
    """Recipient codes chosen by the laboratory. Nothing in here is derived from LIMS data."""

    data_provider: str = ""
    sampling_company_code: str = ""
    lab_name_code: str = ""
    task_code: str = ""
    matrix_codes: dict = field(default_factory=dict)  # snapshot matrix label -> rt_matrix code
    tests: dict = field(default_factory=dict)  # TestDefinition id -> {TEST_FIELDS keys: value}
    qualifiers: dict = field(default_factory=dict)  # lab qualifier code -> NYSDEC qualifier
    samples: dict = field(default_factory=dict)  # lab ID -> depth/location context for a version
    results: dict = field(default_factory=dict)  # str(revision id) -> {"test_type": ...}
    qc_matrix_codes: dict = field(default_factory=dict)  # batch matrix key -> LAB rt_matrix code
    qc_samples: dict = field(
        default_factory=dict
    )  # QC sample code -> {sample_date, dilution_factor}


@dataclass(frozen=True)
class RevisionFacts:
    """Immutable facts about a reported revision that the snapshot does not carry."""

    definition_id: int
    definition_label: str
    first_analysis_started_at: datetime  # revision 1 of the same result (aware instant)
    first_prepared_aliquot_id: int | None = None


@dataclass(frozen=True)
class Issue:
    section: str
    record: str
    field: str
    message: str
    level: str = "error"
    target: tuple | None = None  # ("project",) | ("test", definition id) | ("qualifier", code)
    lab_id: str = ""  # laboratory sample the finding belongs to, when it belongs to one


@dataclass(frozen=True)
class QCFacts:
    """Immutable facts of a QC measurement referenced by the frozen QC evidence."""

    definition_id: int
    retest_unknown: bool  # supersedes an earlier measurement with a different analysis start


@dataclass
class Export:
    tables: dict  # section -> list of row dicts (column order from SECTIONS)
    issues: list
    manifest: dict = field(default_factory=dict)  # included / omitted / interpretations

    @property
    def errors(self):
        return [i for i in self.issues if i.level == "error"]

    @property
    def ok(self):
        return not self.errors

    def counts(self):
        return {name: len(rows) for name, rows in self.tables.items()}


def edd_datetime(iso):
    """Frozen ISO timestamp -> MM/DD/YYYY HH:MM in the offset it was frozen with (no conversion)."""
    if not iso:
        return ""
    return datetime.fromisoformat(iso).strftime("%m/%d/%Y %H:%M")


def _preparation_fields(preparation, test, record, issues, analysis_date, sample_date):
    """Explicit recipient mapping; validate chronology at the EDD's minute precision."""
    method = str(test.get("prep_method", "")).strip()
    if (
        not method
        or test.get("preparation_method_code") != preparation.get("method_code")
        or test.get("preparation_method_version") != preparation.get("method_version")
    ):
        issues.append(
            Issue(
                "Settings",
                record,
                "prep_method",
                "Map this exact laboratory preparation method and version to an explicit "
                "NYSDEC rt_prep_method code.",
            )
        )
    date = edd_datetime(preparation.get("prepared_at"))
    _check_prep_date(date, analysis_date, sample_date, record, issues, required=True)
    return {"prep_method": method, "prep_date": date}


def _check_prep_date(date, analysis_date, sample_date, record, issues, required=False):
    if not date:
        if required:
            issues.append(
                Issue(
                    "TestResultQC_v5", record, "prep_date", "Record the actual preparation start."
                )
            )
        return
    try:
        value = datetime.strptime(date, "%m/%d/%Y %H:%M")
        analysis = datetime.strptime(analysis_date, "%m/%d/%Y %H:%M")
        collection = datetime.strptime(sample_date, "%m/%d/%Y %H:%M") if sample_date else None
    except ValueError:
        return  # the normal date/required-field validator reports malformed context
    if value >= analysis or (collection and value < collection):
        issues.append(
            Issue(
                "TestResultQC_v5",
                record,
                "prep_date",
                "Preparation must be on/after collection and strictly before analysis at the "
                "EDD minute precision. Correct the source chronology; times are not invented.",
            )
        )


def build(snapshot, mapping, revisions, qc_facts=None):
    """Return an Export for one released version. `revisions` maps revision id -> RevisionFacts;
    `qc_facts` maps QC measurement id -> QCFacts (lab QC rows need them)."""
    content = snapshot["content"]
    sdg = content["report"]["number"]
    issues, samples, results, batches = [], [], [], []

    def error(section, record, name, message, lab_id=""):
        issues.append(Issue(section, record, name, message, lab_id=lab_id))

    for name, label in PROJECT_FIELDS.items():
        if not getattr(mapping, name).strip():
            issues.append(
                Issue(
                    "Settings",
                    "Project",
                    name,
                    f"{label} is not set for this project.",
                    target=("project",),
                )
            )
    seen_codes = {}
    for s in content["samples"]:
        record = f"Sample {s['lab_id']}"
        if not s["results"]:
            continue
        code = s["client_sample_id"].strip()
        if code in seen_codes:
            error(
                "Sample_v5",
                record,
                "sys_sample_code",
                f"Client sample ID {code!r} is also used by {seen_codes[code]}; NYSDEC "
                "sample codes must be unique in an EDD.",
                s["lab_id"],
            )
        seen_codes[code] = s["lab_id"]
        matrix = mapping.matrix_codes.get(s["matrix"], "").strip()
        if not matrix:
            issues.append(
                Issue(
                    "Settings",
                    record,
                    "sample_matrix_code",
                    f"No NYSDEC matrix code is set for {s['matrix']} samples in this project.",
                    target=("project",),
                )
            )
        context = mapping.samples.get(s["lab_id"], {})
        location = context.get("sys_loc_code", "")
        if not location:
            issues.append(
                Issue(
                    "Context",
                    record,
                    "sys_loc_code",
                    "NYSDEC requires a location code for every normal sample (EDD Manual 4.4.1). "
                    "Enter the site's NYSDEC location code in this version's export context.",
                    target=("context",),
                )
            )
        elif location == code:
            issues.append(
                Issue(
                    "Context",
                    record,
                    "sys_loc_code",
                    "The location code must differ from the sample code.",
                    target=("context",),
                )
            )
        start, end = context.get("start_depth", ""), context.get("end_depth", "")
        unit = context.get("depth_unit", "")
        if matrix.upper() in DEPTH_MATRICES and not (start and end and unit):
            issues.append(
                Issue(
                    "Context",
                    record,
                    "start_depth",
                    f"NYSDEC requires start depth, end depth and depth unit for matrix {matrix} "
                    "normal samples. Enter them in this version's export context.",
                    target=("context",),
                )
            )
        elif (start or end) and not unit:
            issues.append(
                Issue("Context", record, "depth_unit", "Enter the depth unit.", target=("context",))
            )
        try:
            if start and end and Decimal(end) <= Decimal(start):
                issues.append(
                    Issue(
                        "Context",
                        record,
                        "end_depth",
                        "End depth must be greater than start depth.",
                        target=("context",),
                    )
                )
        except InvalidOperation:
            pass  # reported by the numeric format check
        samples.append(
            {
                "data_provider": mapping.data_provider,
                "sys_sample_code": code,
                "sample_name": code,
                "sample_matrix_code": matrix,
                "sample_type_code": "N",
                "sample_source": "FIELD",
                "sample_date": edd_datetime(s["collected_at"]),
                "sys_loc_code": location,
                "start_depth": start,
                "end_depth": end,
                "depth_unit": unit,
                "chain_of_custody": s.get("coc_reference", ""),
                "sample_receipt_date": edd_datetime(s["received_at"]),
                "sampler": s.get("collector", ""),
                "sampling_company_code": mapping.sampling_company_code,
                "task_code": mapping.task_code,
                "composite_yn": "Y" if s.get("collection_started_at") else "N",
                "composite_desc": (
                    f"Composite collected {edd_datetime(s['collection_started_at'])} to "
                    f"{edd_datetime(s['collected_at'])}"
                    if s.get("collection_started_at")
                    else ""
                ),
            }
        )
        for r in s["results"]:
            rrecord = f"{s['lab_id']} {r['analyte']}"
            if r.get("source") == "external":
                # Fail closed: NYSDEC needs the performing lab's registered code, analysis location
                # and batch linkage, which this export does not yet support for subcontracted work.
                # Never substitute this laboratory's code or drop the row silently.
                lab = (r.get("external") or {}).get("laboratory", "an external laboratory")
                error(
                    "TestResultQC_v5",
                    rrecord,
                    "lab_name_code",
                    f"This result was subcontracted to {lab}. NYSDEC export of subcontracted "
                    "results is not supported yet; it needs the performing laboratory's "
                    "registered code and batch information.",
                    s["lab_id"],
                )
                continue
            facts = revisions.get(r["revision_id"])
            if facts is None:
                error("TestResultQC_v5", rrecord, "", "The reported result revision is missing.")
                continue
            test = mapping.tests.get(facts.definition_id, {})
            own_basis = BASIS_CODES.get(r.get("result_basis") or "")
            missing = [
                label
                for key, label in TEST_FIELDS.items()
                if not str(test.get(key, "")).strip() and not (key == "basis" and own_basis)
            ]  # a result's own recorded basis is authoritative: the mapping basis is not needed
            if missing:
                issues.append(
                    Issue(
                        "Settings",
                        f"Test {facts.definition_label}",
                        "mapping",
                        "Set the NYSDEC mapping for this test: " + ", ".join(missing) + ".",
                        target=("test", facts.definition_id),
                    )
                )
            started = datetime.fromisoformat(r["analysis_started_at"])
            same_preparation = (r.get("preparation") or {}).get(
                "aliquot_id"
            ) == facts.first_prepared_aliquot_id
            if r["revision"] == 1 or (
                started == facts.first_analysis_started_at and same_preparation
            ):
                test_type = "INITIAL"
            else:
                test_type = mapping.results.get(str(r["revision_id"]), {}).get("test_type", "")
                if not test_type:
                    issues.append(
                        Issue(
                            "Context",
                            rrecord,
                            "test_type",
                            f"Revision {r['revision']} differs in analysis start from revision "
                            "1 and the LIMS does not record whether it was a reanalysis or a "
                            "re-extraction. Enter the NYSDEC test type in this version's "
                            "export context.",
                            target=("context",),
                        )
                    )
            if not (r.get("cas_number") or "").strip():
                error(
                    "TestResultQC_v5",
                    rrecord,
                    "cas_rn",
                    f"Analyte {r['analyte']} has no CAS number in the catalog.",
                    s["lab_id"],
                )
            lab_codes = list(r["qualifiers"])
            interpreted = ""
            if r["kind"] == "non_detect":
                if "U" not in lab_codes:
                    lab_codes.insert(0, "U")
                interpreted = "U"
            elif lab_codes:
                unmapped = [c for c in lab_codes if not mapping.qualifiers.get(c)]
                for code in unmapped:
                    issues.append(
                        Issue(
                            "Settings",
                            f"Qualifier {code}",
                            "interpreted_qualifiers",
                            f"Set the NYSDEC interpreted qualifier for lab qualifier {code}.",
                            target=("qualifier", code),
                        )
                    )
                interpreted = "".join(mapping.qualifiers.get(c, "") for c in lab_codes)
            unit = str(test.get("unit_code", ""))
            method = str(test.get("lab_anl_method_name", ""))
            fraction = str(test.get("fraction", ""))
            analysis_date = edd_datetime(r["analysis_started_at"])
            results.append(
                {
                    "sys_sample_code": code,
                    "lab_anl_method_name": method,
                    "analysis_date": analysis_date,
                    "fraction": fraction,
                    "column_number": "NA",
                    "test_type": test_type,
                    "lab_matrix_code": matrix,
                    "analysis_location": "LB",
                    "basis": _result_basis(r, s, test, facts, rrecord, issues),
                    "dilution_factor": r["dilution_factor"],
                    "prep_date": edd_datetime(r.get("prepared_at")),
                    "lab_name_code": mapping.lab_name_code,
                    "qc_level": "QUANT",
                    "lab_sample_id": s["lab_id"],
                    "analyst_name": r.get("analyst") or "",
                    "preservative": str(test.get("preservative", "")),
                    "cas_rn": (r.get("cas_number") or "").strip(),
                    "chemical_name": str(test.get("chemical_name", "")),
                    "result_value": "" if r["kind"] == "non_detect" else r["reported"],
                    "result_unit": unit,
                    "result_type_code": "TRG",
                    "reportable_result": "Yes",
                    "detect_flag": "N" if r["kind"] == "non_detect" else "Y",
                    "lab_qualifiers": "".join(lab_codes),
                    "interpreted_qualifiers": interpreted,
                    "validated_yn": "N",
                    "reporting_detection_limit": r["reporting_limit"],
                    "detection_limit_unit": unit,
                    "result_comment": (r.get("comments") or "")[:255],
                    "lab_sdg": sdg,
                }
            )
            if preparation := r.get("preparation"):
                results[-1].update(
                    _preparation_fields(
                        preparation,
                        test,
                        rrecord,
                        issues,
                        analysis_date,
                        edd_datetime(s["collected_at"]),
                    )
                )
            else:
                _check_prep_date(
                    results[-1]["prep_date"],
                    analysis_date,
                    edd_datetime(s["collected_at"]),
                    rrecord,
                    issues,
                )
            batches.append(
                {
                    "sys_sample_code": code,
                    "lab_anl_method_name": method,
                    "analysis_date": analysis_date,
                    "fraction": fraction,
                    "column_number": "NA",
                    "test_type": test_type,
                    "test_batch_type": "ANALYSIS",
                    "test_batch_id": r["batch"],
                }
            )
            if preparation := r.get("preparation"):
                # NYSDEC §4.6: PREP joins the same analytical test identity. Its date and
                # method remain analysis values; only the independent group ID/type differ.
                batches.append(
                    {
                        **batches[-1],
                        "test_batch_type": "PREP",
                        "test_batch_id": preparation["batch"],
                    }
                )
    cas_by_analyte = {
        r["analyte"]: (r.get("cas_number") or "").strip()
        for s in content["samples"]
        for r in s["results"]
    }
    manifest = _lab_qc(
        content, mapping, qc_facts or {}, cas_by_analyte, sdg, samples, results, batches, issues
    )
    tables = {
        "Sample_v5": [_ordered("Sample_v5", row) for row in samples],
        "TestResultQC_v5": [_ordered("TestResultQC_v5", row) for row in results],
        "Batch_v5": _distinct([_ordered("Batch_v5", row) for row in batches]),
    }
    lab_of = {s["client_sample_id"].strip(): s["lab_id"] for s in content["samples"]}
    issues += validate(tables, DERIVED_FIELDS, lab_of)
    issues.append(Issue("Export", "All", "", NOTICE, level="warning"))
    manifest["included"] = {name: len(rows) for name, rows in tables.items()}
    linked = sum(bool(r.get("preparation")) for s in content["samples"] for r in s["results"])
    unlinked = sum(not r.get("preparation") for s in content["samples"] for r in s["results"])
    manifest["preparation"] = {"linked_results": linked, "unlinked_results": unlinked}
    if linked:
        manifest["omitted"] = [v for v in manifest["omitted"] if not v.startswith("PREP and LEACH")]
        manifest["omitted"].append(
            "Aliquot label, preparation completion/operator/equipment/method version have no "
            "dedicated NYSDEC fields. LEACH and multiple sequential preparation groups "
            "per test are not supported."
        )
    return Export(tables, _dedupe(issues), manifest)


BASIS_CODES = {"wet": "WET", "dry": "DRY", "na": "NA"}


def _result_basis(r, sample, test, facts, record, issues):
    """NYSDEC basis of a normal result. The basis recorded with the result is authoritative and is
    never compared with or replaced by the test-wide mapping: two results of one soil test may be
    wet and dry. The mapping basis serves only legacy results and lab QC. A result with no recorded
    basis is not relabelled: soil is refused, and a legacy aqueous result may use a mapping of NA,
    the only value that is true for it."""
    frozen = BASIS_CODES.get(r.get("result_basis") or "")
    mapped = str(test.get("basis", "")).strip()
    lab_id = sample["lab_id"]
    if frozen:
        return frozen
    if sample["matrix"].lower() == "soil":
        issues.append(
            Issue(
                "TestResultQC_v5",
                record,
                "basis",
                "The wet or dry weight basis of this soil result was not recorded, and it is "
                "not taken from the test mapping. NYSDEC needs it; a corrected result revision "
                "with the basis is required.",
                target=("result", r["result_id"]),
                lab_id=lab_id,
            )
        )
        return ""
    if mapped in ("WET", "DRY"):
        issues.append(
            Issue(
                "TestResultQC_v5",
                record,
                "basis",
                f"This result has no recorded basis and the test mapping says {mapped}; "
                "only NA is true for a result without a basis.",
                target=("test", facts.definition_id),
                lab_id=lab_id,
            )
        )
        return ""
    return mapped


def _distinct(rows):
    """Batch_v5 is keyed per sample analysis, not per analyte: analytes sharing a sample, method,
    time and batch give one identical row. A same-key row naming another batch stays and fails the
    key check."""
    out, seen = [], set()
    for row in rows:
        key = tuple(row.items())
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _ordered(section, row):
    unknown = set(row) - {c.name for c in SECTIONS[section]}
    assert not unknown, f"{section} has no column {unknown}"
    return {
        c.name: "" if row.get(c.name) is None else str(row.get(c.name, ""))
        for c in SECTIONS[section]
    }


_DATETIME = re.compile(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}")


# Columns whose value comes only from recipient settings, test/qualifier mappings or the version
# export context. When one is empty, build() has already reported a targeted issue naming where
# to fix it, so the generic "required" check would only duplicate it under the wrong fix location.
DERIVED_FIELDS = frozenset(
    {
        "data_provider",
        "sampling_company_code",
        "task_code",
        "lab_name_code",
        "sample_matrix_code",
        "lab_matrix_code",
        "lab_anl_method_name",
        "fraction",
        "preservative",
        "basis",
        "chemical_name",
        "test_type",
        "cas_rn",
        "dilution_factor",
        "sample_date",
    }
)


def validate(tables, derived=frozenset(), lab_of=None):
    """Format checks the application can make: required, type, length, enumeration, keys.
    Empty `derived` columns are skipped for "required" only (build() reports their cause).
    `lab_of` maps a sample code to its laboratory sample ID so findings can name the record."""
    issues = []
    lab_of = lab_of or {}
    for section, rows in tables.items():
        columns = SECTIONS[section]
        for index, row in enumerate(rows, start=1):
            record = f"{section} row {index} ({row.get('sys_sample_code', '')})"
            lab_id = lab_of.get(row.get("sys_sample_code", ""), "")
            for c in columns:
                value = row[c.name]
                if not value:
                    if c.required and c.name not in derived:
                        issues.append(
                            Issue(section, record, c.name, f"{c.name} is required.", lab_id=lab_id)
                        )
                    continue
                if c.kind == "text" and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    issues.append(
                        Issue(
                            section,
                            record,
                            c.name,
                            "Text beginning with =, +, -, @, tab or carriage return could be "
                            "interpreted as a spreadsheet formula. Correct the source value "
                            "before export; values are never rewritten automatically.",
                            lab_id=lab_id,
                        )
                    )
                if "\t" in value or "\n" in value or "\r" in value:
                    issues.append(
                        Issue(
                            section,
                            record,
                            c.name,
                            "Tabs and line breaks cannot be exported.",
                            lab_id=lab_id,
                        )
                    )
                if c.kind == "text" and c.length and len(value) > c.length:
                    issues.append(
                        Issue(
                            section,
                            record,
                            c.name,
                            f"{c.name} {value!r} is {len(value)} characters; "
                            f"NYSDEC allows {c.length}.",
                            lab_id=lab_id,
                        )
                    )
                elif c.kind == "numeric":
                    try:
                        Decimal(value)
                    except InvalidOperation:
                        issues.append(
                            Issue(
                                section,
                                record,
                                c.name,
                                f"{c.name} {value!r} is not a number.",
                                lab_id=lab_id,
                            )
                        )
                elif c.kind == "datetime" and not _DATETIME.fullmatch(value):
                    issues.append(
                        Issue(
                            section,
                            record,
                            c.name,
                            f"{c.name} must be MM/DD/YYYY HH:MM.",
                            lab_id=lab_id,
                        )
                    )
                if c.enum and value not in ENUMERATIONS[c.enum]:
                    issues.append(
                        Issue(
                            section,
                            record,
                            c.name,
                            f"{c.name} {value!r} is not one of {', '.join(ENUMERATIONS[c.enum])}.",
                            lab_id=lab_id,
                        )
                    )
        keys = [c.name for c in columns if c.name in PRIMARY_KEYS[section]]
        seen = set()
        for index, row in enumerate(rows, start=1):
            key = tuple(row[k] for k in keys)
            if key in seen:
                issues.append(
                    Issue(
                        section,
                        f"{section} row {index}",
                        ", ".join(keys),
                        "Duplicate primary key; NYSDEC requires one row per key.",
                    )
                )
            seen.add(key)
    return issues


PRIMARY_KEYS = {
    "Sample_v5": {"sys_sample_code"},
    "TestResultQC_v5": {
        "sys_sample_code",
        "lab_anl_method_name",
        "analysis_date",
        "fraction",
        "column_number",
        "test_type",
        "cas_rn",
    },
    "Batch_v5": {
        "sys_sample_code",
        "lab_anl_method_name",
        "analysis_date",
        "fraction",
        "column_number",
        "test_type",
        "test_batch_type",
    },
}


def _dedupe(issues):
    out, seen = [], set()
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            out.append(issue)
    return out


def write_zip(tables):
    """ZIP of tab-delimited section files with the official template's two '#' header rows.
    Values are pre-validated to contain no tabs or line breaks, so no quoting is needed
    (5.3.1: an empty field is a null). Fixed member timestamps make identical inputs give
    identical bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for section in SECTION_ORDER:
            columns = SECTIONS[section]
            lines = [
                "#" + "\t".join(c.name for c in columns),
                "#" + "\t".join(_type(c) for c in columns),
            ]
            lines += ["\t".join(row[c.name] for c in columns) for row in tables[section]]
            text = "".join(line + "\r\n" for line in lines)
            info = zipfile.ZipInfo(f"{section}.txt", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode("utf-8"))
    return buffer.getvalue()


def _type(column):
    if column.kind == "text":
        return f"Text({column.length})"
    return "Numeric" if column.kind == "numeric" else "DateTime"


LAB_QC_TYPES = {"method_blank": "LB", "lcs": "BS"}  # rt_sample_type: Lab Blank, Blank Spike
OMITTED_QC = {
    "matrix_spike": "Matrix spike rows are not exported (parent sample linkage and client "
    "confidentiality rules not yet implemented).",
    "matrix_spike_duplicate": "Matrix spike duplicate rows are not exported (as matrix spikes).",
    "lab_duplicate": "Laboratory duplicate rows are not exported (parent sample linkage not yet "
    "implemented).",
    "calibration_verification": "Calibration verification is instrument QC, not an EDD sample; "
    "not exported.",
}
ALWAYS_OMITTED = [
    "Surrogate, internal standard and TIC rows: not recorded by the LIMS.",
    "Method detection limit and quantitation limit: not recorded per result; left blank.",
    "PREP and LEACH batch rows: unlinked legacy timestamps are not inferred as groups.",
    "Earlier (non-reportable) tests of corrected results: only the released revision is exported.",
    "Validation fields (validator qualifiers, validation level): data are unvalidated "
    "(validated_yn N).",
]
INTERPRETATIONS = [
    "Lab QC sample_date and QC dilution factor are the values entered in this version's export "
    "context (the manual does not define a lab QC sample date; nothing is defaulted).",
    "LCS qc_original_conc is left blank: Appendix A says the LCS original concentration is "
    "assumed to be zero.",
    "LCS qc_spike_recovery and control limits are the values analytical evaluated and the reviewer "
    "relied on (frozen QC evidence); qc_spike_status '*' marks a recovery outside limits.",
    "Lab QC sampling_company_code is the laboratory code: the laboratory generated the QC sample.",
    "Result basis is the wet or dry basis recorded with each result, whatever the test mapping "
    "says. A legacy aqueous result without a recorded basis is exported as NA only when the test "
    "mapping says NA. Lab QC rows use the test mapping basis, which must be set for them.",
]
_QC_CODE = re.compile(r"[A-Za-z0-9._-]+")


def _plain(text):
    if text in (None, ""):
        return ""
    value = format(Decimal(str(text)), "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _lab_qc(content, mapping, qc_facts, cas_by_analyte, sdg, samples, results, batches, issues):
    """Method blank and LCS rows from the frozen, client-scoped QC evidence. Returns the manifest's
    omissions and interpretations; appends rows and issues in place."""
    zone = ZoneInfo(content["lab"]["timezone"])
    omitted, seen = {}, set()
    for batch in content["batches"]:
        qc_matrix = mapping.qc_matrix_codes.get(batch["matrix"], "").strip()
        for analyte, evidence in batch["qc"].items():
            for m in evidence.get("measurements", []):
                if m.get("withheld"):
                    omitted["withheld"] = (
                        "QC prepared from samples not in this report is withheld "
                        "(client confidentiality); not exported."
                    )
                    continue
                if m["qc_type"] not in LAB_QC_TYPES:
                    omitted[m["qc_type"]] = OMITTED_QC.get(
                        m["qc_type"], f"{m['qc_type']}: not exported."
                    )
                    continue
                code = f"{batch['number']}-{m['qc_sample']}"
                record = f"QC {code} {analyte}"
                if not _QC_CODE.fullmatch(code):
                    issues.append(
                        Issue(
                            "Sample_v5",
                            record,
                            "sys_sample_code",
                            "QC sample codes may use letters, digits, '-', '_' and '.' "
                            "only; rename the QC sample label.",
                        )
                    )
                context = mapping.qc_samples.get(code, {})
                if not qc_matrix:
                    issues.append(
                        Issue(
                            "Settings",
                            "Project",
                            "qc_matrix_code",
                            f"No NYSDEC lab QC matrix code is set for {batch['matrix']} "
                            "batches in this project.",
                            target=("project",),
                        )
                    )
                for key, label in (
                    ("sample_date", "sample date"),
                    ("dilution_factor", "dilution factor"),
                ):
                    if not context.get(key):
                        issues.append(
                            Issue(
                                "Context",
                                f"QC {code}",
                                key,
                                f"Enter the lab QC {label} in this version's export context.",
                                target=("context",),
                            )
                        )
                if code not in seen:
                    seen.add(code)
                    samples.append(
                        {
                            "data_provider": mapping.data_provider,
                            "sys_sample_code": code,
                            "sample_name": code,
                            "sample_matrix_code": qc_matrix,
                            "sample_type_code": LAB_QC_TYPES[m["qc_type"]],
                            "sample_source": "LAB",
                            "sample_date": edd_datetime(context.get("sample_date", "")),
                            "sampling_company_code": mapping.lab_name_code,
                            "task_code": mapping.task_code,
                            "composite_yn": "N",
                        }
                    )
                facts = qc_facts.get(m["measurement_id"])
                if facts is None:
                    issues.append(Issue("TestResultQC_v5", record, "", "QC measurement missing."))
                    continue
                test = mapping.tests.get(facts.definition_id, {})
                if not str(test.get("basis", "")).strip():
                    issues.append(
                        Issue(
                            "Settings",
                            record,
                            "basis",
                            "Lab QC has no recorded basis of its own, so it uses the test "
                            "mapping basis (WET, DRY or NA). Set it for this test.",
                            target=("test", facts.definition_id),
                        )
                    )
                if facts.retest_unknown:
                    issues.append(
                        Issue(
                            "TestResultQC_v5",
                            record,
                            "test_type",
                            "This QC measurement replaced one with a different analysis "
                            "start; its NYSDEC test type is not recorded.",
                        )
                    )
                cas = cas_by_analyte.get(analyte, "")
                if not cas:
                    issues.append(
                        Issue(
                            "TestResultQC_v5",
                            record,
                            "cas_rn",
                            f"Analyte {analyte} has no CAS number in the released report.",
                        )
                    )
                detected = m["observed_text"].strip().upper() != "ND"
                unit = str(test.get("unit_code", ""))
                method = str(test.get("lab_anl_method_name", ""))
                fraction = str(test.get("fraction", ""))
                analysis = datetime.fromisoformat(m["analysis_started_at"]).astimezone(zone)
                row = {
                    "sys_sample_code": code,
                    "lab_anl_method_name": method,
                    "analysis_date": analysis.strftime("%m/%d/%Y %H:%M"),
                    "fraction": fraction,
                    "column_number": "NA",
                    "test_type": "" if facts.retest_unknown else "INITIAL",
                    "lab_matrix_code": qc_matrix,
                    "analysis_location": "LB",
                    "basis": str(test.get("basis", "")),
                    "dilution_factor": context.get("dilution_factor", ""),
                    "lab_name_code": mapping.lab_name_code,
                    "qc_level": "QUANT",
                    "lab_sample_id": code,
                    "preservative": str(test.get("preservative", "")),
                    "cas_rn": cas,
                    "chemical_name": str(test.get("chemical_name", "")),
                    "result_value": m["observed_text"].strip() if detected else "",
                    "result_unit": unit,
                    "result_type_code": "TRG",
                    "reportable_result": "Yes",
                    "detect_flag": "Y" if detected else "N",
                    "lab_qualifiers": "" if detected else "U",
                    "interpreted_qualifiers": "" if detected else "U",
                    "validated_yn": "N",
                    "reporting_detection_limit": _plain(m["reporting_limit_applied"]),
                    "detection_limit_unit": unit,
                    "lab_sdg": sdg,
                }
                if m["qc_type"] == "lcs":
                    recovery = next(
                        (e for e in m["evaluation"]["measures"] if e["measure"] == "recovery"), None
                    )
                    if recovery is None or recovery.get("computed") is None:
                        issues.append(
                            Issue(
                                "TestResultQC_v5",
                                record,
                                "qc_spike_recovery",
                                "LCS recovery was not computed (non-detect, missing spike "
                                "or no limit); NYSDEC requires it for LCS rows.",
                            )
                        )
                    row.update(
                        {
                            "qc_spike_added": _plain(m.get("spike_value")),
                            "qc_spike_measured": m["observed_text"].strip() if detected else "0",
                            "qc_spike_recovery": _plain((recovery or {}).get("computed")),
                            "qc_spike_lcl": _plain((recovery or {}).get("lower")),
                            "qc_spike_ucl": _plain((recovery or {}).get("upper")),
                            "qc_spike_status": "*"
                            if (recovery or {}).get("status") == "fail"
                            else "",
                        }
                    )
                results.append(row)
                prep = batch.get("kind") == "preparation"
                analysis_batch = m.get("analysis_batch") if prep else batch["number"]
                if prep:
                    row.update(
                        _preparation_fields(
                            batch,
                            test,
                            record,
                            issues,
                            row["analysis_date"],
                            edd_datetime(context.get("sample_date", "")),
                        )
                    )
                    if not analysis_batch:
                        issues.append(
                            Issue(
                                "Batch_v5",
                                record,
                                "test_batch_id",
                                "Record the actual analytical run for this preparation QC "
                                "measurement, then issue a new report version. "
                                "No ANALYSIS ID is inferred from PREP.",
                            )
                        )
                batches.append(
                    {
                        "sys_sample_code": code,
                        "lab_anl_method_name": method,
                        "analysis_date": row["analysis_date"],
                        "fraction": fraction,
                        "column_number": "NA",
                        "test_type": row["test_type"],
                        "test_batch_type": "ANALYSIS",
                        "test_batch_id": analysis_batch or "",
                    }
                )
                if prep:
                    batches.append(
                        {**batches[-1], "test_batch_type": "PREP", "test_batch_id": batch["number"]}
                    )
    return {"omitted": list(omitted.values()) + ALWAYS_OMITTED, "interpretations": INTERPRETATIONS}
