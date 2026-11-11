"""Screen the recorded cooler receipt temperature against explicit laboratory configuration.

This observation does not establish container temperature, continuous cooling, freezing,
chemical preservation or container suitability. Those require separate documented assessment.
No regulatory threshold or exception is inferred here.
"""

from decimal import Decimal

LABELS = {
    "within": "Within configured temperature range",
    "outside": "Outside configured temperature range",
    "not_required": "No temperature requirement",
    "unconfigured": "Rule not configured",
    "manual": "Manual review required",
}


def _finite_decimal(value):
    return isinstance(value, Decimal) and value.is_finite()


def screen_temperature(*, mode, reading=None, observed_unit="", unit="°C", lower=None, upper=None):
    """Return a finding, never a laboratory acceptance decision.

    None mode means there is no applicable rule. Configured bounds are inclusive. No unit
    conversion, rounding, string parsing or float coercion occurs. Invalid/incomplete inputs
    remain unevaluated rather than producing a numerical pass.
    """

    def finding(status, detail):
        return {"status": status, "label": LABELS[status], "detail": detail}

    if mode is None:
        return finding("unconfigured", "No applicable preservation rule is configured.")
    if mode == "not_required":
        return finding(
            "not_required",
            "The configured rule explicitly has no temperature requirement. "
            "This does not assess other preservation requirements.",
        )
    if mode != "range":
        return finding("manual", "Temperature suitability requires documented manual assessment.")
    bounds = [bound for bound in (lower, upper) if bound is not None]
    if (
        not bounds
        or not all(_finite_decimal(bound) for bound in bounds)
        or (lower is not None and upper is not None and lower > upper)
    ):
        return finding(
            "manual", "The configured numeric temperature bounds are incomplete or invalid."
        )
    if unit != "°C" or observed_unit != unit:
        return finding(
            "manual",
            "The recorded temperature unit is missing or unsupported; no conversion was made.",
        )
    if not _finite_decimal(reading):
        return finding("manual", "An exact finite recorded cooler temperature is required.")
    if (lower is not None and reading < lower) or (upper is not None and reading > upper):
        return finding(
            "outside",
            "The unrounded recorded cooler receipt temperature is outside the configured bounds.",
        )
    return finding(
        "within",
        "The unrounded recorded cooler receipt temperature is within the configured bounds. "
        "Container temperature and preservation history are not established by this screen.",
    )
