"""Synthetic demonstration configuration. NOT a validated or regulatory limit set.

Values exist so the demo journey runs end to end. A lab must replace them with limits taken
from its methods and SOPs before real use. Seeding is initial setup: it creates missing rows
only, never overwrites a lab's edits, and writes no audit events.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError

from catalog.models import Method, TestDefinition
from core.models import Lab

from .models import HoldingTimeRule, PreservationRule, QCLimit, QCRequirement, QCType, Qualifier

BASIS = "SYNTHETIC DEMONSTRATION VALUE. Not validated, not a regulatory limit."

QUALIFIERS = [
    ("J", "Estimated value, below the reporting limit (demo qualifier set)", False),
    ("H", "Holding time exceeded (demo qualifier set)", True),
    ("B", "Analyte found in the associated blank (demo qualifier set)", False),
]

REQUIRED = {QCType.METHOD_BLANK, QCType.LCS}
LIMITS = [  # qc_type, measure, lower, upper (None for blank: upper = reporting limit)
    (QCType.LCS, "recovery", Decimal("80"), Decimal("120")),
    (QCType.CALIBRATION_VERIFICATION, "recovery", Decimal("90"), Decimal("110")),
    (QCType.MATRIX_SPIKE, "recovery", Decimal("75"), Decimal("125")),
    (QCType.MATRIX_SPIKE_DUPLICATE, "recovery", Decimal("75"), Decimal("125")),
    (QCType.MATRIX_SPIKE_DUPLICATE, "rpd", None, Decimal("20")),
    (QCType.LAB_DUPLICATE, "rpd", None, Decimal("20")),
]


def seed_analytical_config(method_codes=None):
    if not Lab.objects.filter(pk=1, name="Example Environmental Lab").exists():
        raise ValidationError("Refusing to seed outside the synthetic demo laboratory.")
    for code, description, holding in QUALIFIERS:
        Qualifier.objects.get_or_create(
            code=code, defaults={"description": description, "indicates_holding_time": holding}
        )
    methods = Method.objects.filter(is_active=True)
    if method_codes:
        methods = methods.filter(code__in=method_codes)
    count = 0
    for method in methods:
        definitions = TestDefinition.objects.filter(method=method, is_active=True)
        for matrix in definitions.values_list("matrix", flat=True).distinct():
            HoldingTimeRule.objects.get_or_create(
                method=method,
                matrix=matrix,
                analyte=None,  # the method-wide rule; analytes may have rules of their own
                start_event="collection",
                end_event="analysis",
                defaults={
                    "name": "Demo: collection to analysis",
                    "duration": timedelta(days=14),
                    "warning_window": timedelta(days=2),
                    "basis": BASIS,
                },
            )
        for qc_type in QCType.values:
            QCRequirement.objects.get_or_create(
                method=method,
                qc_type=qc_type,
                defaults={
                    "min_per_batch": 1,
                    "every_n_samples": 20,
                    "basis": BASIS,
                    "is_active": qc_type in REQUIRED,
                },
            )
        for d in definitions:
            PreservationRule.objects.get_or_create(
                definition=d,
                defaults={
                    "temperature_mode": "range",
                    "lower": Decimal("2"),
                    "upper": Decimal("8"),
                    "basis": BASIS
                    + " Fictional cooler-temperature screen only, not method guidance.",
                },
            )
            if d.reporting_limit is not None:
                QCLimit.objects.get_or_create(
                    definition=d,
                    phase="analysis",
                    qc_type=QCType.METHOD_BLANK,
                    measure="value",
                    defaults={"lower": None, "upper": d.reporting_limit, "basis": BASIS},
                )
            for qc_type, measure, lower, upper in LIMITS:
                QCLimit.objects.get_or_create(
                    definition=d,
                    phase="analysis",
                    qc_type=qc_type,
                    measure=measure,
                    defaults={"lower": lower, "upper": upper, "basis": BASIS},
                )
        count += 1
    return count
