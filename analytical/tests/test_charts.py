"""QC control charts: exact Decimal values, configured vs statistical limits, escaping, pages."""

import statistics
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from analytical import charts
from analytical import services as svc
from analytical.models import QCLimit, QCMeasurement
from analytical.qc import plain

pytestmark = pytest.mark.django_db
D = Decimal


def lcs_history(config, analyst, observed, *, label="LCS-1"):
    """One batch per observed value, each with an LCS for Lead (spike 10), oldest first."""
    lead = config["defs"]["Lead"]
    now = timezone.now()
    for i, text in enumerate(observed):
        batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
        sample = svc.add_qc_sample(actor=analyst, batch_id=batch.pk, qc_type="lcs", label=label)
        svc.record_qc_measurement(
            actor=analyst,
            qc_sample_id=sample.pk,
            definition_id=lead.pk,
            observed_text=text,
            spike_value="10",
            reporting_limit_applied="0.005",
            analysis_started_at=now - timedelta(hours=len(observed) - i),
        )
    return lead


NORMAL = [str(9 + (i % 5) / 10) for i in range(24)]  # 9.0 .. 9.4: recoveries 90 to 94


def test_points_are_exact_recoveries_and_nd_is_not_plotted(config, analyst):
    lead = lcs_history(config, analyst, ["9.8", "ND", "10.25"])
    series = charts.build_series(lead, "lcs", "recovery")
    assert [p.value for p in series.points] == [D("98"), D("102.5")]  # exact, not float
    assert series.not_plotted == {"Not computable (non-detect or missing spike/reference input)": 1}
    assert [p.observed_text for p in series.points] == ["9.8", "10.25"]  # supplied text kept
    assert series.configured == (D("80"), D("120"))  # the lab's limits, as configured


def test_statistics_are_descriptive_exact_and_need_a_baseline_of_twenty(config, analyst):
    few = lcs_history(config, analyst, NORMAL[:10])
    short = charts.build_series(few, "lcs", "recovery")
    assert short.stats is None and "not evaluated" in short.stats_reason
    assert "regulatory threshold" in short.stats_reason and all(p.band == "" for p in short.points)


def test_limits_bands_and_markers_use_decimals_and_leave_configured_limits_alone(config, analyst):
    lead = lcs_history(config, analyst, NORMAL + ["13.5"])  # 135%: also outside 80-120
    before = list(QCLimit.objects.values_list("pk", "lower", "upper"))
    measured = list(QCMeasurement.objects.values_list("pk", "observed_text", "observed_value"))
    series = charts.build_series(lead, "lcs", "recovery", baseline=0)
    values = [p.value for p in series.points]
    mean, sd = statistics.mean(values), statistics.stdev(values)
    assert series.stats["mean"] == mean and series.stats["sd"] == sd  # standard library, Decimal
    assert series.stats["control"] == (mean - 3 * sd, mean + 3 * sd)
    last = series.points[-1]
    assert last.configured_status == "fail" and last.band in ("warning", "control")
    assert series.counts["outside_configured"] == 1
    # reading a chart never changes configured limits or any observation
    assert list(QCLimit.objects.values_list("pk", "lower", "upper")) == before
    assert (
        list(QCMeasurement.objects.values_list("pk", "observed_text", "observed_value")) == measured
    )


def test_payload_separates_limit_kinds_and_carries_exact_text_for_the_chart(config, analyst):
    lead = lcs_history(config, analyst, NORMAL + ["13.5"])  # the last point is outside 80-120
    series = charts.build_series(lead, "lcs", "recovery", baseline=0)
    payload = charts.chart_payload(series)
    kinds = {line["kind"] for line in payload["lines"]}
    assert kinds == {"config", "mean", "warning", "control"}  # configured and statistical apart
    configured = [line for line in payload["lines"] if line["kind"] == "config"]
    assert [(line["label"], line["exact"]) for line in configured] == [
        ("Configured lower limit", "80"),
        ("Configured upper limit", "120"),
    ]
    assert [line["exact"] for line in payload["lines"] if line["kind"] == "control"] == [
        plain(series.stats["control"][1]),
        plain(series.stats["control"][0]),
    ]
    last = payload["points"][-1]
    assert last["shape"] == "rect" and last["status"] == "fail" and last["exact"] == "135"
    assert payload["points"][0]["observed"] == "9.0" and payload["unit"] == "%"
    assert len(payload["points"]) == len(payload["labels"]) == 25
    # floats are display positions only; the exact text is what a person reads
    assert isinstance(last["y"], float) and last["exact"] == plain(series.points[-1].value)


def test_detail_page_hands_chart_js_a_json_payload_and_escapes_labels(
    config, analyst, reviewer, client
):
    lead = lcs_history(config, analyst, NORMAL, label="<b>LCS</b>")
    client.force_login(reviewer)
    page = client.get(
        reverse("analytical:chart-detail", args=[lead.pk, "lcs", "recovery"]), {"baseline": "0"}
    )
    html = page.content.decode()
    assert '<canvas id="control-chart"' in html and 'id="control-chart-data"' in html
    assert html.index("vendor/chartjs-4.6.1/chart.umd.min.js") < html.index(
        "analytical/control_chart.js"
    )  # the library loads before the initializer
    assert "\\u003Cb\\u003ELCS" in html or "\u003cb\u003eLCS" in html  # escaped in the JSON
    assert "<b>LCS</b>" not in html
    assert 'class="vr-chart"' not in html  # the hand-built SVG chart is gone
    assert "Configured limits" in html and "Statistical bands" in html  # kinds stay labelled
    assert "Control chart. The data table below lists every point." in html  # text alternative


def test_chart_pages_authorization_filters_and_invalid_input(config, analyst, reviewer, client):
    lead = lcs_history(config, analyst, NORMAL[:3])
    from accounts.models import User
    from receipt.models import Client as ClientRecord

    portal = User.objects.create_user(
        "portal", "p@example.invalid", "x", client=ClientRecord.objects.first()
    )
    client.force_login(portal)
    assert client.get(reverse("analytical:chart-list")).status_code == 403
    client.force_login(reviewer)
    listing = client.get(reverse("analytical:chart-list"))
    row = listing.context["page_obj"][0]
    assert row["n"] == 3 and row["qc_label"] == "Laboratory control sample"
    assert [k for k, _ in row["measures"]] == ["recovery"]
    assert len(client.get(reverse("analytical:chart-list"), {"q": "zzz"}).context["page_obj"]) == 0
    assert (
        client.get(reverse("analytical:chart-list"), {"matrix": "bogus"})
        .context["filter_form"]
        .errors
    )  # invalid filters: errors and no rows
    url = reverse("analytical:chart-detail", args=[lead.pk, "lcs", "recovery"])
    page = client.get(url)
    assert page.status_code == 200 and b"Configured limits" in page.content
    assert b"Statistical bands are not evaluated" in page.content  # only 3 points
    bad = client.get(url, {"baseline": "7"})
    assert (
        bad.status_code == 200
        and bad.context["series"] is None
        and bad.context["baseline_form"].errors
    )
    for args in (
        (lead.pk, "nope", "recovery"),
        (lead.pk, "lcs", "rpd"),
        (999999, "lcs", "recovery"),
    ):
        assert client.get(reverse("analytical:chart-detail", args=args)).status_code == 404


def test_chart_dates_and_times_are_shown_in_the_labs_zone_not_utc(config, analyst, settings):
    import datetime

    settings.TIME_ZONE = "America/New_York"
    lead = lcs_history(config, analyst, ["9.8", "9.9"])
    # 03:00 UTC on 15 January is 22:00 on 14 January in New York
    QCMeasurement.objects.all().update(
        analysis_started_at=datetime.datetime(2026, 1, 15, 3, 0, tzinfo=datetime.UTC)
    )
    payload = charts.chart_payload(charts.build_series(lead, "lcs", "recovery"))
    assert payload["labels"] == ["2026-01-14 22:00", "2026-01-14 22:00"]


def test_band_notes_cite_only_what_was_read_and_count_excluded_non_detects(
    config, analyst, reviewer, client
):
    lead = config["defs"]["Lead"]
    now = timezone.now()
    for i in range(25):  # 22 detected blanks and 3 non-detects
        batch = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
        sample = svc.add_qc_sample(
            actor=analyst, batch_id=batch.pk, qc_type="method_blank", label="MB-1"
        )
        svc.record_qc_measurement(
            actor=analyst,
            qc_sample_id=sample.pk,
            definition_id=lead.pk,
            observed_text="ND" if i % 8 == 7 else f"0.00{i % 4}",
            reporting_limit_applied="0.005",
            analysis_started_at=now - timedelta(hours=25 - i),
        )
    series = charts.build_series(lead, "method_blank", "value", 0)
    assert series.non_detect_count == 3 and series.counts["points"] == 22
    client.force_login(reviewer)
    html = client.get(
        reverse("analytical:chart-detail", args=[lead.pk, "method_blank", "value"]),
        {"baseline": "0"},
    ).content.decode()
    assert "3 non-detects left out" in html and "say nothing about how often" in html
    assert "sample standard deviations" in html and "sample SD" in html
    assert "not QC limits" in html and "do not set, replace or certify" in html
    assert "as they are" in html and "recomputed every time this page opens" in html
    assert "EPA 815-R-05-004 section 7.2.8" in html and "Procedure 18.1" in html
    assert "Standard Methods" not in html  # that text was not read
    assert "Mean +3 SD (descriptive)" in html  # the drawn lines carry the same wording
