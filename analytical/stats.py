"""Statistical values for MDL studies (40 CFR Part 136 Appendix B, Revision 2).

No distribution is implemented here. For the sample counts the regulation tabulates (Table 1)
the published rounded constant is used and labelled as such. For any other count the value comes
from `scipy.stats.t.ppf`, an established library. Its float64 result is validated (integer
degrees of freedom, finite and positive output) and then rounded half up to the table's three
decimals. If the library is missing or returns an unusable value, the t value is reported as
unavailable and the calculation is "not evaluated".
"""

import math
from decimal import ROUND_HALF_UP, Decimal

# Appendix B Table 1: replicates n -> single-tailed 99th percentile t, n-1 degrees of freedom.
APPENDIX_B_TABLE_1 = {
    7: "3.143",
    8: "2.998",
    9: "2.896",
    10: "2.821",
    11: "2.764",
    16: "2.602",
    21: "2.528",
    26: "2.485",
    31: "2.457",
    32: "2.453",
    48: "2.408",
    50: "2.405",
    61: "2.390",
    64: "2.387",
    80: "2.374",
    96: "2.366",
    100: "2.365",
}
TABLE_1_SOURCE = "40 CFR 136 Appendix B Table 1 (published constant)"
THREE_PLACES = Decimal("0.001")


class TValueUnavailable(Exception):
    """The t value for this sample count cannot be supplied; the calculation is not evaluated."""


class TValueInvalid(TValueUnavailable):
    """The library returned something unusable (not finite, or not positive)."""


def t_quantile_99(n):
    """Return (t, source) for the single-tailed 99th percentile with n-1 degrees of freedom.

    Listed n use the published Table 1 constant. Any other n uses `scipy.stats.t.ppf`, whose
    float64 result is validated (integer degrees of freedom of at least one; finite, positive
    output) and only then rounded half up to Table 1's three decimals. That conversion is a
    bounded numerical step, not an exact one: `source` records the library, its version, the
    degrees of freedom and the rounding, so the value can be reproduced."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 2:
        raise ValueError("The replicate count must be an integer of at least two.")
    if n in APPENDIX_B_TABLE_1:
        return Decimal(APPENDIX_B_TABLE_1[n]), TABLE_1_SOURCE
    try:
        import numpy
        import scipy
        from scipy import stats
    except ImportError as exc:
        raise TValueUnavailable(
            f"The Student t value for n = {n} is not in Appendix B Table 1 and needs the SciPy "
            "library, which is not installed."
        ) from exc
    df = n - 1
    raw = float(stats.t.ppf(0.99, df))
    if not math.isfinite(raw) or raw <= 0:
        raise TValueInvalid(
            f"SciPy returned an unusable Student t value ({raw!r}) for n = {n}; the calculation "
            "is not evaluated."
        )
    value = Decimal(repr(raw)).quantize(THREE_PLACES, rounding=ROUND_HALF_UP)
    return value, (
        f"scipy {scipy.__version__} (numpy {numpy.__version__}) stats.t.ppf(0.99, {df}), "
        "float64 result rounded half up to 3 decimals (the precision of Appendix B Table 1)"
    )
