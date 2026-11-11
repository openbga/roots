"""MDL rules (40 CFR 136 Appendix B Rev. 2) as pure functions: no database."""

import sys
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from analytical import mdl
from analytical.stats import (
    APPENDIX_B_TABLE_1,
    TValueInvalid,
    TValueUnavailable,
    t_quantile_99,
)

D = Decimal
AS_OF = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
LEVEL = D("5")
STORE = D(1).scaleb(-10)


def obs(
    pk,
    kind,
    value,
    *,
    day=0,
    batch=None,
    level=LEVEL,
    ok=True,
    instrument="",
    prep=None,
    assumed=False,
):
    when = AS_OF - timedelta(days=30 + day)
    return mdl.Obs(
        pk=pk,
        kind=kind,
        value=None if value is None else D(str(value)),
        qualitative_ok=ok if kind == "spike" else None,
        spike_level=level if kind == "spike" else None,
        prepared_on=when.date(),
        analyzed_on=when.date(),
        analyzed_at=when,
        batch=batch or f"B{day % 3}",
        preparation=prep or f"P{pk}",
        instrument=instrument,
        prepared_assumed=assumed,
    )


def spikes(values=(1, 2, 3, 4, 5, 6, 7), **kw):
    return [obs(i, "spike", v, day=i, **kw) for i, v in enumerate(values)]


def blanks(values, **kw):
    return [obs(100 + i, "blank", v, day=i, **kw) for i, v in enumerate(values)]


def run(s=None, b=None, **kw):
    kw.setdefault("kind", "initial")
    return mdl.calculate(
        s if s is not None else spikes(),
        b if b is not None else blanks([0.1, 0.2, 0.1, 0.3, 0.2, 0.1, 0.2]),
        study_level=LEVEL,
        as_of=AS_OF,
        **kw,
    )


def manual_sd(values):
    mean = sum(values) / len(values)
    return mean, (sum((v - mean) ** 2 for v in values) / (len(values) - 1)).sqrt()


def q(value):
    return value.quantize(STORE, rounding=ROUND_HALF_UP)


def test_mdl_s_is_t_times_the_sample_standard_deviation_of_exact_decimals():
    out = run()
    mean, sd = manual_sd([D(i) for i in range(1, 8)])
    assert out.status == "calculated" and out.reasons == []
    assert out.spike_mean == q(mean) and out.spike_sd == q(sd)
    assert out.t_spike == D("3.143") and "Table 1" in out.t_spike_source
    assert out.mdl_s == q(D("3.143") * sd)
    assert out.mean_recovery_pct == q(mean / LEVEL * 100)


def test_blank_case_c_all_numerical_uses_mean_plus_t_sd_and_zero_for_a_negative_mean():
    values = [D(x) for x in ("0.1", "0.2", "0.1", "0.3", "0.2", "0.1", "0.2")]
    out = run(b=blanks(values))
    mean, sd = manual_sd(values)
    assert out.mdl_b_method == "mean_t" and out.mdl_b == q(mean + D("3.143") * sd)
    assert out.verified_mdl == max(out.mdl_s, out.mdl_b)
    negative = [D(x) for x in ("-1", "-1.1", "-0.9", "-1", "-1.2", "-1", "-1.1")]
    out = run(b=blanks(negative))
    _, sd = manual_sd(negative)
    assert out.mdl_b == q(D("3.143") * sd)  # zero replaces the negative mean


def test_blank_case_a_none_numerical_means_mdl_b_does_not_apply():
    out = run(b=blanks([None] * 7))
    assert out.mdl_b is None and out.mdl_b_method == "not_applicable"
    assert out.verified_mdl == out.mdl_s  # ND is not a numerical result


def test_blank_case_b_some_numerical_takes_the_highest_result():
    out = run(b=blanks([None, 0.2, None, 0.5, 0.1, None, 0.3]))
    assert out.mdl_b == D("0.5") and out.mdl_b_method == "highest"


def test_blank_percentile_reproduces_the_regulations_164_blank_example():
    values = [None] * 4 + [0.1] * 155 + [1.5, 1.7, 1.9, 5.0, 10]  # 164 blanks, 160 numerical
    out = run(b=blanks(values))
    assert out.mdl_b == D("1.9") and out.mdl_b_method == "percentile"  # 164 x 0.99 -> 162nd


def test_percentile_rank_rounds_half_up_and_only_applies_above_100_blanks():
    # n = 150: 150 x 0.99 = 148.5 -> rank 149 (the higher neighbour, conservative)
    ranked = [None] * 2 + [D(i) / 100 for i in range(1, 149)]
    out = run(b=blanks(ranked))
    assert out.mdl_b == D("1.47") and out.mdl_b_method == "percentile"
    # exactly 100 blanks: the text says "more than 100", so the highest result is used
    exactly = [None] + [D(i) / 100 for i in range(1, 100)]
    out = run(b=blanks(exactly))
    assert out.mdl_b == D("0.99") and out.mdl_b_method == "highest"


def test_not_evaluated_lists_every_reason_and_never_gives_a_partial_mdl():
    few = spikes((1, 2, 3, 4, 5))  # 5 spikes
    out = run(s=few, b=blanks([0.1] * 3))
    text = " ".join(out.reasons)
    assert out.status == "not_evaluated" and out.mdl_s is None and out.verified_mdl is None
    assert (
        "5 distinct preparation(s) of spiked sample(s)" in text
        and "at least 7 preparations" in text
    )
    assert "3 distinct preparation(s) of method blank(s)" in text


@pytest.mark.parametrize(
    "mutation,fragment",
    [
        (dict(values=(1, 2, 3, 4, 5, 6, None)), "numerical result greater than zero"),
        (dict(values=(1, 2, 3, 4, 5, 6, 0)), "numerical result greater than zero"),
        (dict(values=(1, 2, 3, 4, 5, 6, -1)), "numerical result greater than zero"),
    ],
)
def test_spikes_that_are_nd_zero_or_negative_require_a_higher_spiking_level(mutation, fragment):
    out = run(s=spikes(mutation["values"]))
    assert out.status == "not_evaluated" and fragment in " ".join(out.reasons)


def test_failed_qualitative_identification_and_mixed_spiking_levels_are_reasons():
    rows = spikes()
    rows[0] = obs(0, "spike", 1, day=0, ok=False)
    assert "qualitative identification" in " ".join(run(s=rows).reasons)
    rows = spikes()
    rows[1] = obs(1, "spike", 2, day=1, level=D("6"))
    assert "different spiking levels" in " ".join(run(s=rows).reasons)


def test_three_batches_and_three_prepared_and_analysed_dates_are_required():
    one_day = [obs(i, "spike", v, day=0, batch="B1") for i, v in enumerate(range(1, 8))]
    text = " ".join(run(s=one_day).reasons)
    assert "1 batch(es)" in text and "prepared on 1 recorded calendar date" in text
    assert "analysed on 1 calendar date" in text


def test_spikes_older_than_24_months_are_not_used():
    old = [mdl.Obs(**{**o.__dict__, "analyzed_at": AS_OF - timedelta(days=800)}) for o in spikes()]
    assert "older than 24 months" in " ".join(run(s=old).reasons)


def test_several_instruments_each_need_two_samples_on_different_dates():
    rows = spikes()
    rows = [
        mdl.Obs(**{**o.__dict__, "instrument": "ICP-1" if i < 6 else "ICP-2"})
        for i, o in enumerate(rows)
    ]
    bl = [mdl.Obs(**{**o.__dict__, "instrument": "ICP-1"}) for o in blanks([0.1] * 7)]
    text = " ".join(run(s=rows, b=bl).reasons)
    assert "Instrument ICP-2: needs at least 2 spiked" in text
    assert "Instrument ICP-2: needs at least 2 blank" in text
    unlabeled = " ".join(
        run(s=rows[:6] + [mdl.Obs(**{**rows[6].__dict__, "instrument": ""})], b=bl).reasons
    )
    assert "some observations have no instrument" in unlabeled


def test_retention_rule_ratio_between_half_and_two_and_under_three_percent_of_blanks():
    base = run(existing_mdl=None)
    existing = (base.verified_mdl / D("1.2")).quantize(STORE)
    ok = run(existing_mdl=existing, b=blanks([0.1] * 7), kind="verification")
    assert ok.ratio_to_existing is not None and ok.retention_eligible is True
    too_far = run(
        existing_mdl=(ok.verified_mdl / 3).quantize(STORE), b=blanks([0.1] * 7), kind="verification"
    )
    assert too_far.retention_eligible is False  # ratio above 2.0
    tiny = D("0.05")  # one numerical blank above it is 1/7 = 14%, not under 3%
    with_blank = run(existing_mdl=tiny, b=blanks([0.1] + [None] * 6), kind="verification")
    assert with_blank.blanks_above_existing == 1 and with_blank.retention_eligible is False


def test_listed_sample_counts_use_the_published_constants():
    for n, text in APPENDIX_B_TABLE_1.items():
        value, source = t_quantile_99(n)
        assert value == D(text) and "Table 1" in source


def test_unlisted_count_without_the_library_is_not_evaluated_never_approximated(monkeypatch):
    monkeypatch.setitem(sys.modules, "scipy", None)
    monkeypatch.setitem(sys.modules, "scipy.stats", None)
    with pytest.raises(TValueUnavailable, match="needs the SciPy library"):
        t_quantile_99(12)
    out = run(s=spikes(tuple(range(1, 13))))  # n = 12 spikes
    assert out.status == "not_evaluated" and "SciPy" in " ".join(out.reasons)
    assert out.mdl_s is None


def test_scipy_agrees_with_every_published_table_value():
    import scipy.stats as scipy_stats  # a declared dependency: absent means fail, never skip

    for n, text in APPENDIX_B_TABLE_1.items():
        computed = D(repr(float(scipy_stats.t.ppf(0.99, n - 1)))).quantize(
            D("0.001"), rounding=ROUND_HALF_UP
        )
        assert computed == D(text), n
    value, source = t_quantile_99(12)
    assert value == D("2.718") and "scipy" in source


def test_month_arithmetic_clamps_to_month_end_and_recalculation_is_13_months():
    assert mdl.months_before(datetime(2026, 3, 31), 1) == datetime(2026, 2, 28)
    assert mdl.months_after(datetime(2026, 1, 31), 13) == datetime(2027, 2, 28)
    assert mdl.months_before(datetime(2026, 11, 9), 24) == datetime(2024, 11, 9)


def test_blank_window_options_pick_all_24_months_or_the_larger_recent_set():
    rows = [
        mdl.Obs(**{**b.__dict__, "analyzed_at": AS_OF - timedelta(days=15 * i), "pk": i})
        for i, b in enumerate(blanks([0.1] * 60))
    ]  # one blank every 15 days, 900 days back; 24 months keeps ~48
    all_24 = mdl.select_blank_window(rows, AS_OF, "24_months")
    assert 40 <= len(all_24) <= 50 and all(
        r.analyzed_at >= mdl.months_before(AS_OF, 24) for r in all_24
    )
    recent = mdl.select_blank_window(rows, AS_OF, "recent")
    last_six = [r for r in all_24 if r.analyzed_at >= mdl.months_before(AS_OF, 6)]
    assert len(recent) == max(len(last_six), min(50, len(all_24)))


def test_an_unlisted_count_uses_scipy_with_the_version_recorded():
    import scipy  # noqa: F401  declared dependency

    out = run(s=spikes(tuple(range(1, 13))))  # n = 12 is not in Table 1
    mean, sd = manual_sd([D(i) for i in range(1, 13)])
    assert out.status == "calculated" and out.t_spike == D("2.718")
    assert "scipy" in out.t_spike_source and "stats.t.ppf(0.99, 11)" in out.t_spike_source
    assert out.mdl_s == q(D("2.718") * sd)


# ---- review findings M1, M2, Q1 to Q3 -----------------------------------------------------


def extracts(values, instruments=("ICP-1", "ICP-2"), kind="spike"):
    """Each value is one extract, prepared on its own day and analysed on every instrument."""
    rows, pk = [], 0
    for i, value in enumerate(values):
        for instrument in instruments:
            pk += 1
            rows.append(
                obs(pk, kind, value, day=i, instrument=instrument, prep=f"E{i}", batch=f"B{i % 3}")
            )
    return rows


def test_one_extract_analysed_on_two_instruments_is_one_preparation_not_two():
    four = extracts((1, 2, 3, 4))  # 8 analyses, 4 preparations
    out = run(s=four, b=extracts((0.1,) * 7, kind="blank"))
    text = " ".join(out.reasons)
    assert out.status == "not_evaluated" and out.mdl_s is None
    assert "4 distinct preparation(s) of spiked sample(s) from 8 analyses" in text
    assert out.spike_n == 8 and out.spike_preparations == 4


def test_seven_extracts_on_two_instruments_pass_and_the_statistics_use_every_analysis():
    values = (1, 2, 3, 4, 5, 6, 7)
    out = run(s=extracts(values), b=extracts((0.1,) * 7, kind="blank"))
    analyses = [D(v) for v in values for _ in range(2)]  # 14 analyses
    mean, sd = manual_sd(analyses)
    assert out.status == "calculated", out.reasons
    assert out.spike_n == 14 and out.spike_preparations == 7
    assert out.spike_mean == q(mean) and out.spike_sd == q(sd)  # II.2(d)(i): all analyses
    assert out.mdl_s == q(out.t_spike * sd) and out.t_spike == D("2.650")  # t(13, 0.99) from SciPy


def test_retention_of_an_existing_mdl_belongs_to_ongoing_verification_only():
    base = run().verified_mdl
    initial = run(existing_mdl=base, kind="initial")
    assert initial.existing_mdl is None and initial.retention_eligible is None
    assert initial.ratio_to_existing is None and initial.blanks_above_existing is None
    verification = run(existing_mdl=base, kind="verification")
    assert verification.existing_mdl == base and verification.retention_eligible is True


def many_spikes(total, failing, **kw):
    rows = [obs(i, "spike", 1 + i % 7, day=i, **kw) for i in range(total - failing)]
    rows += [obs(900 + i, "spike", None, day=i, **kw) for i in range(failing)]
    return rows


def test_failing_spikes_initial_repeat_higher_but_verification_cites_the_five_percent_rule():
    initial = " ".join(run(s=many_spikes(10, 1)).reasons)
    assert "repeat the spikes at a higher concentration (II.2(c))" in initial
    within = " ".join(run(s=many_spikes(40, 1), kind="verification").reasons)  # 2.5%
    assert "1 of 40 spiked sample(s) (2.5%)" in within and "within the 5% spiking-level" in within
    assert "does not say how such a result enters the standard deviation" in within
    assert "repeat the spikes" not in within  # not a prompt to exclude or to re-spike
    over = " ".join(run(s=many_spikes(40, 3), kind="verification").reasons)  # 7.5%
    assert "more than 5%" in over and "initial MDL re-determined" in over
    assert run(s=many_spikes(40, 1), kind="verification").status == "not_evaluated"  # fails closed


def test_assumed_preparation_dates_cannot_prove_the_required_spread():
    rows = [mdl.Obs(**{**o.__dict__, "prepared_assumed": True}) for o in blanks([0.1] * 7)]
    out = run(b=rows)
    text = " ".join(out.reasons)
    assert out.status == "not_evaluated"
    assert (
        "prepared on 0 recorded calendar date(s)" in text
        and "7 observation(s) have no recorded" in text
    )
    some = [
        mdl.Obs(**{**o.__dict__, "prepared_assumed": i > 1})
        for i, o in enumerate(blanks([0.1] * 7))
    ]
    assert "prepared on 2 recorded calendar date(s)" in " ".join(run(b=some).reasons)


# ---- numerical guards around SciPy -----------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 1, -3, 7.0, 12.5, "12", None, True])
def test_the_replicate_count_must_be_an_integer_of_at_least_two(bad):
    with pytest.raises(ValueError, match="integer of at least two"):
        t_quantile_99(bad)


@pytest.mark.parametrize("unusable", [float("nan"), float("inf"), float("-inf"), 0.0, -1.5])
def test_an_unusable_library_value_is_never_converted_it_makes_the_calculation_not_evaluated(
    monkeypatch, unusable
):
    import scipy.stats

    monkeypatch.setattr(scipy.stats.t, "ppf", lambda q, df: unusable)
    with pytest.raises(TValueInvalid, match="unusable Student t value"):
        t_quantile_99(12)
    out = run(s=spikes(tuple(range(1, 13))))  # n = 12 is not in Table 1
    assert out.status == "not_evaluated" and out.mdl_s is None and out.verified_mdl is None
    assert "unusable Student t value" in " ".join(out.reasons)
    assert t_quantile_99(11)[0] == D("2.764")  # a listed count never reaches the library


def test_scipy_values_are_reproducible_and_consistent_with_the_inverse():
    import numpy
    import scipy
    from scipy import stats

    known = {12: "2.718", 13: "2.681", 14: "2.650", 40: "2.426", 200: "2.345"}
    for n, text in known.items():  # not Table 1 counts: standard 0.99 quantiles
        value, source = t_quantile_99(n)
        assert value == D(text), (n, value)
        assert f"scipy {scipy.__version__}" in source and f"numpy {numpy.__version__}" in source
        assert f"stats.t.ppf(0.99, {n - 1})" in source and "rounded half up to 3 decimals" in source
        assert abs(stats.t.cdf(float(value), n - 1) - 0.99) < 5e-4  # inverse consistency
    assert t_quantile_99(10_000)[0] > D("2.326")  # tends to the normal 0.99 quantile from above
