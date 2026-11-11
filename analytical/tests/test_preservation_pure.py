"""Synthetic numeric bounds exercise comparisons, not regulatory method acceptance."""

from decimal import Decimal

import pytest

from analytical.preservation import screen_temperature


def screen(**changes):
    return screen_temperature(
        **{
            "mode": "range",
            "reading": Decimal("6.10"),
            "observed_unit": "°C",
            "upper": Decimal("6.0"),
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("reading", "status"),
    [("6.10", "outside"), ("6.0000000001", "outside"), ("6.00", "within"), ("5.99", "within")],
)
def test_no_rounding_down_to_satisfy_a_configured_bound(reading, status):
    assert screen(reading=Decimal(reading))["status"] == status


def test_lower_and_upper_bounds_are_independent_and_inclusive():
    assert screen(reading=Decimal("2"), lower=Decimal("2"))["status"] == "within"
    assert screen(reading=Decimal("1.99"), lower=Decimal("2"))["status"] == "outside"
    assert screen(reading=Decimal("100"), lower=Decimal("2"), upper=None)["status"] == "within"


def test_absent_rule_and_explicit_no_temperature_requirement_are_distinct():
    assert screen(mode=None)["status"] == "unconfigured"
    assert screen(mode="not_required", reading=None)["status"] == "not_required"
    assert screen(mode="manual")["status"] == "manual"
    assert "not established" in screen(reading=Decimal("5"))["detail"]


@pytest.mark.parametrize("reading", [None, 6.0, "6.0", Decimal("NaN"), Decimal("Infinity")])
def test_missing_nonfinite_or_lossy_measurement_is_never_a_pass(reading):
    assert screen(reading=reading)["status"] == "manual"


@pytest.mark.parametrize("unit", ["", "F", "°F", "unknown"])
def test_units_are_not_assumed_or_converted(unit):
    assert screen(observed_unit=unit)["status"] == "manual"


@pytest.mark.parametrize(
    ("lower", "upper"),
    [(None, None), (Decimal("7"), Decimal("6")), (None, 6.0), (None, Decimal("NaN"))],
)
def test_invalid_numeric_policy_is_not_a_pass(lower, upper):
    assert screen(lower=lower, upper=upper)["status"] == "manual"
