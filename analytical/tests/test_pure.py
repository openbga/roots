"""Pure-logic tests: parsing, holding-time evaluation, QC evaluation. No database."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError

from analytical.holding import evaluate_rule
from analytical.parsing import parse_decimal, parse_result_text, parse_timestamp
from analytical.qc import FAIL, NOT_EVALUATED, PASS, evaluate_measurement

D = Decimal


@pytest.mark.parametrize(
    "text", ["1,000", "<0.5", "1.5 mg/L", "NaN", "inf", "", " ", "0x10", "1.23456789012", "1e30"]
)
def test_numbers_that_need_coercion_are_rejected(text):
    with pytest.raises(ValidationError):
        parse_result_text(text)


def test_decimal_text_is_exact_and_nd_is_not_zero():
    assert parse_result_text("1.50") == ("detected", D("1.50"))
    assert parse_result_text("0") == ("detected", D("0"))
    assert parse_result_text(" nd ") == ("non_detect", None)
    assert parse_decimal("1e-3") == D("0.001")


def test_comparison_error_explains_nd():
    with pytest.raises(ValidationError, match="Enter ND"):
        parse_result_text("<0.5")


def test_timestamps_require_clear_meaning():
    assert parse_timestamp("2026-11-08T10:00-05:00", "America/New_York").utcoffset() == timedelta(
        hours=-5
    )
    assert parse_timestamp("2026-07-01T10:00", "America/New_York").utcoffset() == timedelta(
        hours=-4
    )
    with pytest.raises(ValidationError, match="ambiguous or does not exist"):
        parse_timestamp("2026-11-01T01:30", "America/New_York")
    with pytest.raises(ValidationError, match="ambiguous or does not exist"):
        parse_timestamp("2026-03-08T02:30", "America/New_York")
    with pytest.raises(ValidationError):
        parse_timestamp("08/11/2026", "America/New_York")


def rule(**kw):
    base = dict(
        pk=1,
        name="r",
        start_event="collection",
        end_event="analysis",
        duration=timedelta(hours=48),
        warning_window=timedelta(hours=6),
    )
    return SimpleNamespace(**{**base, **kw})


T0 = datetime(2026, 1, 1, 8, tzinfo=UTC)


def test_holding_limit_is_to_start_of_analysis_and_inclusive():
    exact = evaluate_rule(rule(), collected_at=T0, analysis_at=T0 + timedelta(hours=48))
    over = evaluate_rule(rule(), collected_at=T0, analysis_at=T0 + timedelta(hours=48, minutes=1))
    assert (exact.status, over.status) == ("met", "exceeded")


def test_running_clock_states_and_missing_prep():
    now = T0 + timedelta(hours=40)
    assert evaluate_rule(rule(), collected_at=T0, now=now).status == "within"
    assert evaluate_rule(rule(), collected_at=T0, now=T0 + timedelta(hours=43)).status == "at_risk"
    assert evaluate_rule(rule(), collected_at=T0, now=T0 + timedelta(hours=49)).status == "expired"
    prep = rule(start_event="preparation")
    assert evaluate_rule(prep, collected_at=T0, analysis_at=T0).status == "not_evaluable"


LIM = {"recovery": (D("80"), D("120"))}


def lcs(observed, spike="10", limits=LIM, kind="detected"):
    return evaluate_measurement(
        "lcs",
        kind=kind,
        observed=None if kind == "non_detect" else D(observed),
        reporting_limit=D("0.1"),
        spike=D(spike),
        native=None,
        reference=None,
        limits=limits,
    )


def test_recovery_bounds_are_inclusive_and_unrounded():
    assert lcs("8").status == PASS and lcs("12").status == PASS
    assert lcs("7.999999").status == FAIL
    assert lcs("12.000001").status == FAIL


def test_missing_limit_or_nd_recovery_is_not_evaluated_never_pass():
    assert lcs("10", limits={}).status == NOT_EVALUATED
    assert lcs("0", kind="non_detect").status == NOT_EVALUATED


def test_matrix_spike_recovery_subtracts_native():
    ev = evaluate_measurement(
        "matrix_spike",
        kind="detected",
        observed=D("15"),
        reporting_limit=D("0.1"),
        spike=D("10"),
        native=D("5"),
        reference=None,
        limits={"recovery": (D("75"), D("125"))},
    )
    assert ev.status == PASS and ev.measures[0].computed == D("100")
    missing = evaluate_measurement(
        "matrix_spike",
        kind="detected",
        observed=D("15"),
        reporting_limit=D("0.1"),
        spike=D("10"),
        native=None,
        reference=None,
        limits={"recovery": (D("75"), D("125"))},
    )
    assert missing.status == NOT_EVALUATED


def test_rpd_and_blank_rules():
    lim = {"rpd": (None, D("20"))}
    dup = lambda a, b: evaluate_measurement(  # noqa: E731
        "lab_duplicate",
        kind="detected",
        observed=D(a),
        reporting_limit=D("0.1"),
        spike=None,
        native=None,
        reference=D(b),
        limits=lim,
    )
    assert dup("10", "12").measures[0].computed.quantize(D("0.01")) == D("18.18")
    assert dup("10", "13").status == FAIL
    assert dup("0", "0").status == PASS
    blank = lambda kind, obs, rl: evaluate_measurement(  # noqa: E731
        "method_blank",
        kind=kind,
        observed=obs,
        reporting_limit=D(rl),
        spike=None,
        native=None,
        reference=None,
        limits={"value": (None, D("0.5"))},
    )
    assert blank("non_detect", None, "0.5").status == PASS
    assert blank("non_detect", None, "1.0").status == FAIL
    assert blank("detected", D("0.6"), "0.1").status == FAIL
