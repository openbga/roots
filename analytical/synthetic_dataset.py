"""Small SYNTHETIC water, wastewater and soil method dataset for demonstrations and tests.

Every method, reporting limit, holding time, QC frequency and QC limit below is an invented
demonstration value. Nothing here is a validated method, an approved regulatory limit or a
claim about any program or accreditation. A lab replaces all of it with values from its own
methods and SOPs. Method codes start with DEMO- so they cannot be mistaken for real methods.
"""

from datetime import timedelta
from decimal import Decimal

from catalog.models import Analyte, Method, TestDefinition

from .models import (
    HoldingTimeRule,
    PreparationRequirement,
    PreservationRule,
    QCLimit,
    QCRequirement,
    QCType,
    Qualifier,
)

VERSION = "demo-1"
SOURCE = (
    "SYNTHETIC DEMONSTRATION DATASET. Invented values for training and tests; not a "
    "validated or approved method and not a regulatory limit."
)
BASIS = "SYNTHETIC DEMONSTRATION VALUE. Not validated, not a regulatory limit."

AQUEOUS = ("water", "wastewater")

QUALIFIERS = [
    ("J", "Estimated value below the reporting limit (demonstration set)", False),
    ("H", "Holding time exceeded (demonstration set)", True),
    ("B", "Analyte detected in the associated blank (demonstration set)", False),
    ("D", "Result from a diluted analysis (demonstration set)", False),
]

# analytes: name, CAS (blank if unsure), {matrix: (unit, reporting limit)}
# hold: (hours, warning hours) per matrix group. qc: type -> (active, min per batch, every N)
# limits: lcs/ccv/ms recovery (lower, upper) percent; rpd upper percent.
# Catalog descriptors for the invented methods and analytes (set when a row is first created).
TECHNIQUES = {
    "DEMO-METALS-AQ": "icp_ms",
    "DEMO-METALS-SOIL": "icp_ms",
    "DEMO-HG-AQ": "cvaa",
    "DEMO-NUTRIENTS-AQ": "colorimetric",
    "DEMO-BOD-AQ": "electrode",
    "DEMO-TSS-AQ": "gravimetric",
}
ANALYTE_GROUPS = {
    "Lead": "metals",
    "Copper": "metals",
    "Zinc": "metals",
    "Mercury": "metals",
    "Nitrate as N": "nutrients",
    "Total phosphorus": "nutrients",
    "BOD, 5-day": "demand",
    "Total suspended solids": "solids",
}
METHODS = [
    {
        "code": "DEMO-METALS-AQ",
        "title": "Metals by ICP-MS, aqueous (synthetic demonstration)",
        "analytes": [
            ("Lead", "7439-92-1", {m: ("ug/L", "1.0") for m in AQUEOUS}),
            ("Copper", "7440-50-8", {m: ("ug/L", "2.0") for m in AQUEOUS}),
            ("Zinc", "7440-66-6", {m: ("ug/L", "10.0") for m in AQUEOUS}),
        ],
        "hold": {m: (24 * 180, 24 * 14) for m in AQUEOUS},
        "qc": {
            "method_blank": (True, 1, 20),
            "lcs": (True, 1, 20),
            "matrix_spike": (False, 1, 20),
            "matrix_spike_duplicate": (False, 1, 20),
            "lab_duplicate": (False, 1, 20),
            "calibration_verification": (False, 1, 10),
        },
        "limits": {"lcs": ("85", "115"), "ccv": ("90", "110"), "ms": ("70", "130"), "rpd": "20"},
    },
    {
        "code": "DEMO-HG-AQ",
        "title": "Mercury by cold vapor AA, aqueous (synthetic demonstration)",
        "analytes": [("Mercury", "7439-97-6", {m: ("ug/L", "0.20") for m in AQUEOUS})],
        "hold": {m: (24 * 28, 24 * 3) for m in AQUEOUS},
        "qc": {
            "method_blank": (True, 1, 20),
            "lcs": (True, 1, 20),
            "matrix_spike": (True, 1, 20),
            "matrix_spike_duplicate": (True, 1, 20),
            "lab_duplicate": (False, 1, 20),
            "calibration_verification": (False, 1, 10),
        },
        "limits": {"lcs": ("80", "120"), "ccv": ("90", "110"), "ms": ("75", "125"), "rpd": "20"},
    },
    {
        "code": "DEMO-NUTRIENTS-AQ",
        "title": "Nitrate and phosphorus, aqueous (synthetic demonstration)",
        "analytes": [
            ("Nitrate as N", "14797-55-8", {m: ("mg/L", "0.05") for m in AQUEOUS}),
            ("Total phosphorus", "7723-14-0", {m: ("mg/L", "0.02") for m in AQUEOUS}),
        ],
        "hold": {m: (48, 6) for m in AQUEOUS},
        "qc": {
            "method_blank": (True, 1, 20),
            "lcs": (True, 1, 20),
            "matrix_spike": (True, 1, 20),
            "matrix_spike_duplicate": (False, 1, 20),
            "lab_duplicate": (True, 1, 20),
            "calibration_verification": (False, 1, 10),
        },
        "limits": {"lcs": ("90", "110"), "ccv": ("90", "110"), "ms": ("80", "120"), "rpd": "15"},
    },
    {
        "code": "DEMO-BOD-AQ",
        "title": "Five-day oxygen demand, aqueous (synthetic demonstration)",
        "analytes": [("BOD, 5-day", "", {m: ("mg/L", "2.0") for m in AQUEOUS})],
        "hold": {m: (48, 6) for m in AQUEOUS},
        "qc": {
            "method_blank": (True, 1, 20),
            "lcs": (True, 1, 20),
            "matrix_spike": (False, 1, 20),
            "matrix_spike_duplicate": (False, 1, 20),
            "lab_duplicate": (True, 1, 10),
            "calibration_verification": (False, 1, 10),
        },
        "limits": {"lcs": ("80", "120"), "ccv": ("90", "110"), "ms": ("75", "125"), "rpd": "25"},
    },
    {
        "code": "DEMO-TSS-AQ",
        "title": "Total suspended solids, aqueous (synthetic demonstration)",
        "analytes": [("Total suspended solids", "", {m: ("mg/L", "5.0") for m in AQUEOUS})],
        "hold": {m: (24 * 7, 24) for m in AQUEOUS},
        "qc": {
            "method_blank": (True, 1, 20),
            "lcs": (False, 1, 20),
            "matrix_spike": (False, 1, 20),
            "matrix_spike_duplicate": (False, 1, 20),
            "lab_duplicate": (True, 1, 10),
            "calibration_verification": (False, 1, 10),
        },
        "limits": {"lcs": ("85", "115"), "ccv": ("90", "110"), "ms": ("75", "125"), "rpd": "10"},
    },
    {
        "code": "DEMO-METALS-SOIL",
        "title": "Metals by ICP-MS, soil (synthetic demonstration)",
        "analytes": [
            ("Lead", "7439-92-1", {"soil": ("mg/kg", "1.0")}),
            ("Copper", "7440-50-8", {"soil": ("mg/kg", "2.0")}),
            ("Zinc", "7440-66-6", {"soil": ("mg/kg", "5.0")}),
        ],
        "hold": {"soil": (24 * 180, 24 * 14)},
        "qc": {
            "method_blank": (True, 1, 20),
            "lcs": (True, 1, 20),
            "matrix_spike": (True, 1, 20),
            "matrix_spike_duplicate": (False, 1, 20),
            "lab_duplicate": (False, 1, 20),
            "calibration_verification": (False, 1, 10),
        },
        "limits": {"lcs": ("80", "120"), "ccv": ("90", "110"), "ms": ("70", "130"), "rpd": "25"},
    },
]


def _limit_rows(spec, reporting_limit):
    """(qc_type, measure, lower, upper) rows for one test definition."""
    lim = spec["limits"]
    lcs, ccv, ms = (tuple(Decimal(x) for x in lim[k]) for k in ("lcs", "ccv", "ms"))
    rpd = Decimal(lim["rpd"])
    return [
        (QCType.METHOD_BLANK, "value", None, Decimal(reporting_limit)),
        (QCType.LCS, "recovery", *lcs),
        (QCType.CALIBRATION_VERIFICATION, "recovery", *ccv),
        (QCType.MATRIX_SPIKE, "recovery", *ms),
        (QCType.MATRIX_SPIKE_DUPLICATE, "recovery", *ms),
        (QCType.MATRIX_SPIKE_DUPLICATE, "rpd", None, rpd),
        (QCType.LAB_DUPLICATE, "rpd", None, rpd),
    ]


def seed_synthetic_dataset():
    """Create the synthetic catalog and analytical configuration where missing.

    Idempotent and non-destructive: existing rows, including edits a lab has made, are never
    overwritten, and nothing is deleted. Returns counts of rows created.
    """
    created = {
        "methods": 0,
        "tests": 0,
        "rules": 0,
        "requirements": 0,
        "limits": 0,
        "qualifiers": 0,
        "preservation_rules": 0,
        "preparation_requirements": 0,
    }

    def make(model, lookup, defaults, key):
        obj, was_created = model.objects.get_or_create(defaults=defaults, **lookup)
        created[key] += was_created
        return obj

    for code, description, holding in QUALIFIERS:
        make(
            Qualifier,
            {"code": code},
            {"description": description, "indicates_holding_time": holding},
            "qualifiers",
        )
    for spec in METHODS:
        method = make(
            Method,
            {"code": spec["code"], "version": VERSION},
            {
                "title": spec["title"],
                "source": SOURCE,
                "reference": "lab",
                "technique": TECHNIQUES.get(spec["code"], ""),
            },
            "methods",
        )
        for name, cas, by_matrix in spec["analytes"]:
            analyte = Analyte.objects.get_or_create(
                name=name, defaults={"cas_number": cas, "group": ANALYTE_GROUPS.get(name, "")}
            )[0]
            for matrix, (unit, rl) in by_matrix.items():
                definition = make(
                    TestDefinition,
                    {"method": method, "analyte": analyte, "matrix": matrix},
                    {"unit": unit, "reporting_limit": Decimal(rl)},
                    "tests",
                )
                make(
                    PreservationRule,
                    {"definition": definition},
                    {
                        "temperature_mode": "range",
                        "lower": Decimal("2"),
                        "upper": Decimal("8"),
                        "basis": BASIS
                        + " Fictional cooler-temperature screen only, not method guidance.",
                    },
                    "preservation_rules",
                )
                for qc_type, measure, lower, upper in _limit_rows(spec, rl):
                    make(
                        QCLimit,
                        {
                            "definition": definition,
                            "phase": "analysis",
                            "qc_type": qc_type,
                            "measure": measure,
                        },
                        {"lower": lower, "upper": upper, "basis": BASIS},
                        "limits",
                    )
        for matrix, (hours, warn) in spec["hold"].items():
            make(
                HoldingTimeRule,
                {
                    "method": method,
                    "matrix": matrix,
                    "start_event": "collection",
                    "end_event": "analysis",
                },
                {
                    "name": "Demo: collection to analysis",
                    "duration": timedelta(hours=hours),
                    "warning_window": timedelta(hours=warn),
                    "basis": BASIS,
                },
                "rules",
            )
        for qc_type, (active, minimum, every) in spec["qc"].items():
            make(
                QCRequirement,
                {"method": method, "qc_type": qc_type},
                {
                    "min_per_batch": minimum,
                    "every_n_samples": every,
                    "basis": BASIS,
                    "is_active": active,
                },
                "requirements",
            )
    prep_method = make(
        Method,
        {"code": "DEMO-DIGEST-SOIL", "version": VERSION},
        {"title": "Independent soil digestion (synthetic demonstration)", "source": SOURCE},
        "methods",
    )
    for definition in TestDefinition.objects.filter(
        method__code="DEMO-METALS-SOIL", method__version=VERSION, matrix="soil"
    ):
        make(
            PreparationRequirement,
            {"definition": definition},
            {"method": prep_method, "basis": BASIS},
            "preparation_requirements",
        )
        for qc_type, measure, lower, upper in _limit_rows(
            next(s for s in METHODS if s["code"] == "DEMO-METALS-SOIL"), definition.reporting_limit
        ):
            make(
                QCLimit,
                {
                    "definition": definition,
                    "phase": "preparation",
                    "qc_type": qc_type,
                    "measure": measure,
                },
                {"lower": lower, "upper": upper, "basis": BASIS},
                "limits",
            )
    for qc_type in ("method_blank", "lcs"):
        make(
            QCRequirement,
            {"method": prep_method, "qc_type": qc_type},
            {"min_per_batch": 1, "every_n_samples": 20, "basis": BASIS},
            "requirements",
        )
    return created
