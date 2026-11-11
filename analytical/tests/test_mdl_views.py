"""MDL pages: list/detail/forms, authorization, independent review, grid contract."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from analytical import mdl_services as svc

from .test_lists import _grid_tables
from .test_mdl_workflow import SPIKES, fill, study_for

pytestmark = pytest.mark.django_db


def stamp(days):
    return (timezone.localtime() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


def test_create_form_validates_inline_and_needs_the_analyst_role(config, client, analyst, reviewer):
    lead = config["defs"]["Lead"]
    url = reverse("analytical:mdl-create")
    client.force_login(reviewer)
    assert client.get(url).status_code == 403
    client.force_login(analyst)
    bad = client.post(url, {"definition": lead.pk, "kind": "initial", "spiking_level": "0"})
    assert bad.status_code == 200 and bad.context["form"].errors["spiking_level"]
    assert b"Spiking level" in bad.content
    ok = client.post(url, {"definition": lead.pk, "kind": "initial", "spiking_level": "5.0"})
    assert ok.status_code == 302 and ok.url.startswith("/analytical/mdl/")


def test_observation_form_requires_identification_for_spikes_and_keeps_input(
    config, client, analyst
):
    study = study_for(config, analyst)
    client.force_login(analyst)
    url = reverse("analytical:mdl-observation-add", args=[study.pk])
    base = {
        "kind": "spike",
        "supplied_text": "4.8",
        "spike_level": "5",
        "prepared_at": stamp(5),
        "analyzed_at": stamp(5),
        "batch_label": "S1",
        "preparation_label": "E1",
    }
    missing = client.post(url, base)
    assert missing.status_code == 200 and "qualitative_ok" in missing.context["form"].errors
    assert b'value="4.8"' in missing.content  # typed input preserved
    garbage = client.post(url, {**base, "qualitative_ok": "yes", "supplied_text": "<0.5"})
    assert garbage.context["form"].errors["supplied_text"]
    again = client.post(url, {**base, "qualitative_ok": "yes", "again": "1"})
    assert again.status_code == 302 and again.url == url
    assert study.observations.get().supplied_text == "4.8"


def test_list_detail_calculation_and_independent_review_through_the_pages(
    config, client, analyst, reviewer
):
    study = study_for(config, analyst)
    fill(study, analyst)
    client.force_login(analyst)
    detail = client.get(reverse("analytical:mdl-detail", args=[study.pk]))
    assert detail.status_code == 200 and b"Ready" in detail.content
    assert b"conservative interpretations, not stated requirements" in detail.content
    posted = client.post(
        reverse("analytical:mdl-calculate", args=[study.pk]), {"blank_window": "24_months"}
    )
    assert posted.status_code == 302
    record = client.get(posted.url)
    assert b"Calculated" in record.content and b"Awaiting review" in record.content
    assert (
        b"separately, so a study can be" in record.content
    )  # the stricter spread reading is stated
    assert (
        b'data-vr-local-form="Record decision"' not in record.content
    )  # an analyst cannot decide it
    calc = study.calculations.get()
    client.force_login(reviewer)
    page = client.get(posted.url)
    assert page.status_code == 200 and b'data-vr-local-form="Record decision"' in page.content
    empty = client.post(posted.url, {"decision": "rejected", "note": ""})
    assert empty.status_code == 200 and empty.context["form"].errors  # rejection needs a reason
    done = client.post(posted.url, {"decision": "adopted", "note": ""})
    assert done.status_code == 302
    calc.refresh_from_db()
    assert calc.decision == "adopted" and calc.reviewed_by == reviewer
    listing = client.get(reverse("analytical:mdl-list"))
    row = listing.context["page_obj"][0]
    assert row.current_mdl == calc.adopted_mdl and row.spike_count == 7 and row.blank_count == 7
    assert b"Current MDL in place" in listing.content
    approved = client.get(reverse("analytical:mdl-list"), {"state": "approved"})
    pending = client.get(reverse("analytical:mdl-list"), {"state": "no_calculation"})
    assert len(approved.context["page_obj"]) == 1 and len(pending.context["page_obj"]) == 0


def test_not_evaluated_calculations_explain_why_and_offer_no_decision(
    config, client, analyst, reviewer
):
    study = study_for(config, analyst)
    fill(study, analyst, spikes=("1", "2", "3"), blanks=("0.1",) * 3)
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    client.force_login(reviewer)
    page = client.get(reverse("analytical:mdl-calculation", args=[calc.pk]))
    assert b"No MDL was produced" in page.content and b"at least 7" in page.content
    assert b'data-vr-local-form="Record decision"' not in page.content
    assert b"cannot be adopted" in page.content


def test_the_calculator_sees_why_they_cannot_decide(config, client, both, analyst):
    study = study_for(config, analyst)
    fill(study, analyst)
    calc = svc.calculate(actor=both, study_id=study.pk)
    client.force_login(both)
    page = client.get(reverse("analytical:mdl-calculation", args=[calc.pk]))
    assert (
        b"You analysed this. Another qualified reviewer must review it." in page.content
        and b'data-vr-local-form="Record decision"' not in page.content
    )
    refused = client.post(
        reverse("analytical:mdl-calculation", args=[calc.pk]), {"decision": "adopted", "note": ""}
    )
    assert refused.status_code == 200 and b"cannot review it" in refused.content
    calc.refresh_from_db()
    assert calc.decision == ""


def test_a_reviewer_who_excluded_an_input_is_not_offered_the_decision(
    config, client, analyst, reviewer
):
    """The page and the service agree: excluding data shapes the result, so the excluder is a
    contributor and sees why they cannot decide instead of a form the service refuses."""
    study = study_for(config, analyst)
    fill(study, analyst, spikes=(*SPIKES, "8"))
    extra = study.observations.get(kind="spike", supplied_text="8")
    svc.exclude_observation(actor=reviewer, observation_id=extra.pk, reason="Injector fault")
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    assert calc.status == "calculated"
    client.force_login(reviewer)
    page = client.get(reverse("analytical:mdl-calculation", args=[calc.pk]))
    assert page.context["self_conflict"] is True
    assert b'data-vr-local-form="Record decision"' not in page.content
    assert b"Another qualified reviewer must review it." in page.content


def test_exclusion_page_needs_a_reason_and_closed_studies_refuse_changes(config, client, analyst):
    study = study_for(config, analyst)
    fill(study, analyst)
    victim = study.observations.filter(kind="spike").first()
    client.force_login(analyst)
    url = reverse("analytical:mdl-exclude", args=[victim.pk])
    blank = client.post(url, {"reason": " "})
    assert blank.status_code == 200 and blank.context["form"].errors
    assert client.post(url, {"reason": "Cracked vial"}).status_code == 302
    client.post(reverse("analytical:mdl-close", args=[study.pk]))
    closed = client.post(
        reverse("analytical:mdl-calculate", args=[study.pk]), {"blank_window": "24_months"}
    )
    assert b"This study is closed" in closed.content and study.calculations.count() == 0
    assert client.get(reverse("analytical:mdl-observation-add", args=[study.pk])).status_code == 200
    refused = client.post(
        reverse("analytical:mdl-observation-add", args=[study.pk]),
        {
            "kind": "blank",
            "supplied_text": "ND",
            "prepared_at": stamp(1),
            "analyzed_at": stamp(1),
            "batch_label": "Z",
        },
    )
    assert refused.status_code == 200 and study.observations.filter(batch_label="Z").count() == 0


def test_new_grids_meet_the_column_adapter_contract(config, client, analyst, reviewer):
    study = study_for(config, analyst)
    fill(study, analyst)
    from .test_charts import lcs_history

    lcs_history(config, analyst, ["9.8", "9.9"])
    from analytical import import_profiles as ip

    ip.create_profile(
        actor=analyst,
        name="Grid example",
        layout="long",
        delimiter="comma",
        mapping={
            "header_row": 1,
            "date_format": "iso",
            "sample": "S",
            "analysis_started_at": "T",
            "analyte": "A",
            "result": "R",
            "unit": {"fixed": "mg/L"},
            "reporting_limit": {"fixed": "0.005"},
            "dilution_factor": {"fixed": "1"},
        },
    )
    client.force_login(reviewer)
    seen = set()
    for name in ("analytical:mdl-list", "analytical:chart-list", "analytical:import-profile-list"):
        for table in _grid_tables(client.get(reverse(name)).content.decode()):
            ids = table["head"][0]
            assert all(ids) and len(ids) == len(set(ids)), (table["key"], ids)
            assert all(len(row) == len(ids) for row in table["rows"]) and table["span"] == 0
            seen.add(table["key"])
    assert seen == {"analytical.mdl_studies", "analytical.charts", "analytical.import_profiles"}


def test_forms_have_breadcrumbs_and_meaningful_empty_choices(config, client, analyst):
    client.force_login(analyst)
    pages = {
        "analytical:mdl-create": ("MDL studies", "Choose a test"),
        "analytical:batch-create": ("Batches &amp; QC", "Choose a method"),
    }
    for name, (crumb, empty) in pages.items():
        html = client.get(reverse(name)).content.decode()
        assert 'aria-label="Breadcrumb"' in html and crumb in html, name
        assert f">{empty}</option>" in html, name
        assert "---------" not in html, name
    limit = client.get(reverse("analytical:config-new", args=["qc-limit"])).content.decode()
    assert ">Choose a test</option>" in limit and "---------" not in limit


def test_recalculation_deadline_counts_from_the_calculation_not_from_late_adoption(
    config, client, analyst, reviewer, monkeypatch
):
    """Appendix B II.4(a): thirteen months from performing the calculation. A reviewer who adopts
    it two months later does not restart that interval, and a newer unreviewed calculation does
    not change the adopted basis."""
    from analytical import mdl

    study = study_for(config, analyst)
    fill(study, analyst)
    performed = timezone.now()
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    assert calc.status == "calculated"
    clock = {"now": performed + timedelta(days=61)}
    monkeypatch.setattr(timezone, "now", lambda: clock["now"])
    adopted = svc.review_calculation(actor=reviewer, calculation_id=calc.pk, decision="adopted")
    assert adopted.reviewed_at > calc.calculated_at + timedelta(days=60)
    due = mdl.months_after(timezone.localtime(calc.calculated_at), mdl.RECALCULATE_MONTHS)
    late_due = mdl.months_after(timezone.localtime(adopted.reviewed_at), mdl.RECALCULATE_MONTHS)
    assert due < late_due

    def at(moment):  # sessions expire on the patched clock, so sign in again at each time
        clock["now"] = moment
        client.force_login(analyst)

    overdue_url = reverse("analytical:mdl-list"), {"due": "overdue"}

    def listed(due_filter=False):
        response = client.get(overdue_url[0], overdue_url[1] if due_filter else {})
        return list(response.context["page_obj"]), response

    # One minute before the deadline the test is current; one minute after it is past due.
    at(due - timedelta(minutes=1))
    (row,), response = listed()
    assert (
        row.due_at == due
        and not row.overdue
        and b"13 months after calculation of" in response.content
    )
    assert listed(True)[0] == []
    assert client.get(reverse("analytical:mdl-detail", args=[study.pk])).context["overdue"] is False
    at(due + timedelta(minutes=1))
    (row,), _ = listed()
    assert row.due_at == due and row.overdue and row.due_at < late_due
    assert [r.pk for r in listed(True)[0]] == [study.pk]
    detail = client.get(reverse("analytical:mdl-detail", args=[study.pk]))
    assert detail.context["overdue"] is True and detail.context["basis"].pk == calc.pk
    assert detail.context["due_at"] == due and b"do not restart it" in detail.content
    # A newer calculation awaiting review is shown separately and leaves the basis alone.
    at(performed + timedelta(days=200))
    newer = svc.calculate(actor=analyst, study_id=study.pk)
    at(due + timedelta(minutes=1))
    (row,), response = listed()
    assert row.pending_recalculation and row.due_at == due
    detail = client.get(reverse("analytical:mdl-detail", args=[study.pk]))
    assert detail.context["pending_recalculation"].pk == newer.pk
    assert detail.context["current"].pk == calc.pk and b"has not been reviewed" in detail.content


@pytest.mark.parametrize(
    "performed, check",
    [
        ((2025, 1, 30, 12), (2026, 2, 28)),  # a month-end clamps to the 28th
        ((2024, 1, 31, 12), (2025, 2, 28)),
        ((2023, 1, 30, 12), (2024, 2, 29)),  # leap year clamps to the 29th
    ],
)
def test_overdue_filter_uses_the_same_due_date_as_the_row_at_month_ends(
    config, client, analyst, reviewer, monkeypatch, performed, check
):
    """The calendar due date clamps at month ends. The filter and the row must agree on every
    day around it, which an independent 13-month-ago comparison does not."""
    from datetime import datetime

    lab_zone = timezone.get_current_timezone()
    start = datetime(*performed, tzinfo=lab_zone)
    clock = {"now": start}
    monkeypatch.setattr(timezone, "now", lambda: clock["now"])
    study = study_for(config, analyst)
    fill(study, analyst)  # the whole study is entered and calculated on the patched day
    calc = svc.calculate(actor=analyst, study_id=study.pk)
    assert calc.calculated_at == start
    clock["now"] = start + timedelta(hours=1)
    svc.review_calculation(actor=reviewer, calculation_id=calc.pk, decision="adopted")
    due = datetime(*check, performed[3], tzinfo=lab_zone)
    for offset, expect in (
        (timedelta(days=-1), False),
        (timedelta(minutes=-1), False),
        (timedelta(minutes=1), True),
        (timedelta(days=1), True),
    ):
        clock["now"] = due + offset
        client.force_login(analyst)
        row = client.get(reverse("analytical:mdl-list")).context["page_obj"][0]
        filtered = client.get(reverse("analytical:mdl-list"), {"due": "overdue"})
        assert row.due_at == due
        assert row.overdue is expect, offset
        assert bool(filtered.context["page_obj"]) is expect, offset


def test_blank_import_basis_is_preset_and_provenance_is_shown_without_inference(
    config, client, analyst
):
    from analytical.models import MDLObservation, PreparationRequirement

    study = study_for(config, analyst)
    fill(study, analyst)
    client.force_login(analyst)
    url = reverse("analytical:mdl-import-blanks", args=[study.pk])
    assert client.get(url).context["form"].initial["basis"] == "analytical"
    PreparationRequirement.objects.create(
        definition=study.definition,
        method=study.definition.method,
        basis="SYNTHETIC TEST: preparation requirement; not method guidance.",
    )
    assert client.get(url).context["form"].initial["basis"] == "preparation"
    plain = client.get(reverse("analytical:mdl-detail", args=[study.pk]))
    assert b"Basis not recorded" in plain.content  # existing and hand-entered blanks, not inferred
    blank = study.observations.filter(kind="blank").first()
    blank.pk = None
    blank.batch_label, blank.preparation_label = "P-SYN-1", "P-SYN-1"
    blank.source_context = {
        "basis": "preparation",
        "source_batch_kind": "preparation",
        "source_batch": "P-000001",
        "analytical_batch": "B-000002",
        "reason": "",
    }
    MDLObservation.objects.bulk_create([blank])
    shown = client.get(reverse("analytical:mdl-detail", args=[study.pk]))
    assert b"Preparation batch P-000001" in shown.content
    assert b"analysed in B-000002" in shown.content
