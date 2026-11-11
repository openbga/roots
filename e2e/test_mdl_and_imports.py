"""MDL study with an independent review and QC control charts; explicit mapped file imports.

Two journeys. The first records a method detection limit study from exact decimal text, excludes
one observation only with a documented rationale, calculates an immutable snapshot and has a
different person decide it, then reads a QC control chart whose configured limits and descriptive
statistical limits stay separate. The second previews a refused file, then commits a long and a
wide layout file through explicit mappings, keeping the original strings and the provenance.
"""

import hashlib
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import Lab
from django.utils import timezone
from helpers import LAB_TZ, field, grid_search, lcs_history, local_input, sign_in, sign_out
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

SPIKES = ["1.90", "2.10", "2.05", "1.95", "2.20", "1.85", "2.15"]
GROSS_FAILURE = "9.99"  # a documented instrument fault, excluded with a reason
BLANKS = ["0.30", "0.20", "0.10", "0.30", "0.20", "0.10", "0.20", "0.30"]


def test_mdl_study_is_reviewed_independently_and_charts_keep_limit_kinds_apart(
    page: Page, demo: Lab
):
    from accounts.models import User
    from analytical import mdl_services
    from analytical.models import MDLCalculation, MDLObservation

    lcs_history()  # 23 points near 100 % recovery, the last outside the 85-115 % limits

    # ---- analyst: a study for Lead in water, spikes entered as exact text
    sign_in(page, "demo_analyst", demo.password)
    page.get_by_role("link", name="MDL studies").click()
    page.get_by_role("link", name="New MDL study").first.click()
    page.get_by_role("button", name="Create study").click()
    expect(page.locator("#id_definition")).to_have_attribute("aria-invalid", "true")
    option = (
        page.get_by_label(re.compile(r"^Test \(method"))
        .locator("option")
        .filter(has_text=re.compile(r"^Lead · DEMO-METALS-AQ.* · Water$"))
    )
    page.get_by_label(re.compile(r"^Test \(method")).select_option(
        value=option.first.get_attribute("value")
    )
    field(page, "Kind").select_option(label="Initial determination")
    field(page, "Spiking level").fill("2.00")
    page.get_by_role("button", name="Create study").click()
    expect(page.get_by_text(re.compile(r"Created \S+\. Add spiked samples"))).to_be_visible()
    study_url = page.url

    page.get_by_role("link", name="Add observation").click()
    for index, text in enumerate([*SPIKES, GROSS_FAILURE]):
        analysed = local_input(timedelta(days=-(10 - index)))
        field(page, "Sample type").select_option(label="Spiked sample")
        field(page, "Result").fill(text)
        field(page, "Qualitative identification met").select_option(label="Yes")
        field(page, "Prepared").fill(local_input(timedelta(days=-(10 - index), hours=-1)))
        field(page, "Analysed").fill(analysed)
        field(page, "Preparation batch").fill(f"SB-{index % 3 + 1}")
        field(page, "Preparation / extract ID").fill(f"SP-{index + 1}")
        last = index == len(SPIKES)
        page.get_by_role(
            "button", name="Add observation" if last else "Save and add another"
        ).click()
        expect(page.get_by_text("Observation recorded.")).to_be_visible()
    expect(page).to_have_url(study_url)
    # The background already has this URL; wait for the final save to finish before reloading.
    expect(page.locator("#vr-form-dialog")).to_be_hidden()
    # Blanks are routine QC history in practice: set up through the service.
    study = MDLObservation.objects.values_list("study", flat=True).first()
    analyst = User.objects.get(username="demo_analyst")
    now = timezone.now()
    for index, text in enumerate(BLANKS):
        at = now - timedelta(days=10 - index)
        mdl_services.add_observation(
            actor=analyst,
            study_id=study,
            kind="blank",
            supplied_text=text,
            prepared_at=at - timedelta(hours=1),
            analyzed_at=at,
            batch_label=f"BB-{index % 3 + 1}",
            preparation_label=f"BP-{index + 1}",
        )

    # exclusion needs a documented gross failure; the recorded value is never changed
    page.reload()
    page.get_by_role("link", name=re.compile(rf"^Exclude spiked sample {GROSS_FAILURE}")).click()
    page.get_by_role("button", name="Exclude with this rationale").click()
    expect(page.locator("#id_reason")).to_have_attribute("aria-invalid", "true")
    page.locator("#id_reason").fill("Autosampler skipped the rinse; documented instrument fault.")
    page.get_by_role("button", name="Exclude with this rationale").click()
    expect(
        page.get_by_text("Observation excluded. The recorded value is unchanged.")
    ).to_be_visible()
    expect(page.get_by_text(GROSS_FAILURE).first).to_be_visible()  # still on record

    page.get_by_role("link", name="Calculate").click()
    page.get_by_role("button", name="Calculate and keep the snapshot").click()
    expect(page.get_by_text("Calculated. An independent reviewer must decide it.")).to_be_visible()
    calculation_url = page.url
    expect(
        page.get_by_text("Awaiting review by a reviewer who did not enter the data")
    ).to_be_visible()
    sign_out(page)

    # the stored numbers are an independent function of the exact recorded text
    calc = MDLCalculation.objects.get()
    values = [Decimal(text) for text in SPIKES]
    mean = sum(values) / len(values)
    sd = (sum((v - mean) ** 2 for v in values) / (len(values) - 1)).sqrt()
    assert calc.status == "calculated" and calc.spike_n == len(SPIKES)
    assert abs(calc.mdl_s - Decimal(calc.t_spike) * sd) < Decimal("0.000000001")
    assert calc.verified_mdl >= calc.mdl_s
    used = [row["supplied_text"] for row in calc.inputs if row["used"]]
    assert set(SPIKES) <= set(used) and GROSS_FAILURE not in used  # original text, excluded gone
    assert MDLObservation.objects.filter(supplied_text=GROSS_FAILURE, excluded=True).count() == 1

    # ---- reviewer: another person decides; rejecting needs a reason
    sign_in(page, "demo_reviewer", demo.password)
    page.goto(calculation_url)
    expect(page.get_by_text("Inputs as calculated")).to_be_visible()
    for text in SPIKES:
        expect(page.get_by_text(text, exact=True).first).to_be_visible()
    page.get_by_role("button", name="Record decision", exact=True).click()
    expect(page.locator("#vr-form-title")).to_have_text("Review MDL calculation")
    page.get_by_label("Decision").select_option(label="Reject this calculation")
    page.locator("#vr-form-dialog").get_by_role("button", name="Record decision").click()
    assert MDLCalculation.objects.get().decision == ""
    page.get_by_label("Decision").select_option(label="Adopt the verified MDL")
    page.locator("#vr-form-dialog").get_by_role("button", name="Record decision").click()
    expect(page.get_by_text("Decision recorded.")).to_be_visible()
    calc.refresh_from_db()
    assert calc.decision == "adopted" and calc.adopted_mdl == calc.verified_mdl
    page.goto(study_url)
    expect(page.get_by_text("No MDL has been adopted for this test yet.")).to_have_count(0)

    # ---- the QC chart: configured limits decide status; statistical limits only describe
    page.get_by_role("link", name="QC control charts").click()
    grid_search(page, "Filter QC series").fill("Lead")
    page.get_by_role("button", name="Apply").click()
    row = page.locator("table.vr-table tbody tr").filter(has_text="Laboratory control sample")
    row.first.get_by_role("link", name=re.compile("^Percent recovery")).click()
    expect(page.get_by_text("Configured limits", exact=False).first).to_be_visible()
    expect(page.get_by_text("Statistical bands", exact=False).first).to_be_visible()
    facts = page.locator("dl.vr-facts").first
    expect(facts).to_contain_text(re.compile(r"Outside configured limits\s*1"))
    expect(facts).to_contain_text("Control band (mean ± 3 SD, descriptive)")
    expect(page.get_by_role("img", name=re.compile("Control chart of Lead"))).to_be_visible()
    box = page.locator("#control-chart").bounding_box()
    assert box and box["width"] > 200 and box["height"] > 100  # the chart actually rendered
    points = page.get_by_role("row").filter(has_text=re.compile(r"LCS-1"))
    expect(points.first).to_be_visible()
    assert page.get_by_text("1.30").count() >= 1  # exact observed text kept in the data table
    page.locator("#id_baseline").select_option(label="Most recent 20 points")
    page.get_by_role("button", name="Apply chart settings").click()
    expect(page.locator("dl.vr-facts").first).to_contain_text("Baseline")
    assert page.console_errors == []


LONG = (
    "# SCHEMA EXAMPLE: synthetic text written for this test, not instrument output.\n"
    "Sample Name,Element,Conc,Units,RL,DF,Run Time\n"
    "{lab},{lead},5.20,ug/L,1.0,1,{when}\n"
    "{lab},Cu,ND,ug/L,2.0,1,{when}\n"
)
WIDE = (
    "# SCHEMA EXAMPLE: synthetic text written for this test, not instrument output.\n"
    "ID\tRun Time\tPb\tCu\tDilution\n"
    "{lab}\t{when}\t7.50\t\t1\n"
)


def upload(page: Page, name: str, text: str, profile: str):
    page.get_by_label("Mapping profile").select_option(label=profile)
    page.get_by_label(re.compile("^Results file")).set_input_files(
        files=[{"name": name, "mimeType": "text/plain", "buffer": text.encode()}]
    )
    page.get_by_role("button", name="Validate and preview").click()


def test_mapped_imports_preview_first_and_keep_exact_text_and_provenance(page: Page, demo: Lab):
    from helpers import receive_water

    from accounts.models import User
    from analytical import services
    from analytical.models import ResultRevision
    from catalog.models import TestDefinition
    from core.models import AuditEvent
    from receipt.models import RequestedTest

    one, two = receive_water(
        "E2E-IMPORT",
        count=2,
        received_ago=timedelta(hours=3),
        collected_ago=timedelta(hours=5),
    )
    analyst = User.objects.get(username="demo_analyst")
    method = TestDefinition.objects.get(
        method__code="DEMO-METALS-AQ", matrix="water", analyte__name="Lead"
    ).method
    batch = services.create_batch(actor=analyst, method=method, matrix="water")
    services.add_tests_to_batch(
        actor=analyst,
        batch_id=batch.pk,
        requested_test_ids=list(
            RequestedTest.objects.filter(sample__in=[one, two]).values_list("pk", flat=True)
        ),
    )
    when = (timezone.now() - timedelta(hours=1)).replace(second=0, microsecond=0)
    stamp = when.astimezone(LAB_TZ).isoformat()

    sign_in(page, "demo_analyst", demo.password)
    page.goto(f"/analytical/batches/{batch.pk}/import/")
    page.get_by_role("link", name="Import with a mapping").click()
    expect(page.get_by_text("No active import mapping")).to_be_visible()  # nothing is assumed

    # ---- two mappings, described by the operator: nothing is inferred from a header
    # The empty state leads straight to a new mapping (no header cross-link, standard 3.6).
    page.locator("#vr-form-content").get_by_role("link", name="New mapping").click()
    page.get_by_role("button", name="Create mapping").click()
    expect(page.locator("#id_name")).to_have_attribute("aria-invalid", "true")
    field(page, "Profile name").fill("E2E long layout")
    field(page, "Layout").select_option(value="long")
    field(page, "Delimiter").select_option(value="comma")
    field(page, "Header row").fill("2")
    field(page, "Date format in the file").select_option(value="iso")
    field(page, "Sample ID column").fill("Sample Name")
    field(page, "Analysis start column").fill("Run Time")
    field(page, "Analyte column").fill("Element")
    field(page, "Result column").fill("Conc")
    field(page, "Analyte names in the file").fill("Pb 220.353 | Lead\nCu | Copper")
    field(page, "Unit column").fill("Units")
    field(page, "Reporting limit column").fill("RL")
    field(page, "Dilution factor column").fill("DF")
    page.get_by_role("button", name="Create mapping").click()
    expect(page.get_by_text("Saved the mapping profile 'E2E long layout'")).to_be_visible()

    page.get_by_role("link", name="New mapping").first.click()
    field(page, "Profile name").fill("E2E wide layout")
    field(page, "Layout").select_option(value="wide")
    field(page, "Delimiter").select_option(value="tab")
    field(page, "Header row").fill("2")
    field(page, "Date format in the file").select_option(value="iso")
    field(page, "Sample ID column").fill("ID")
    field(page, "Analysis start column").fill("Run Time")
    field(page, "Analyte columns").fill("Pb | Lead | ug/L | 1.0\nCu | Copper | ug/L | 2.0")
    field(page, "Dilution factor column").fill("Dilution")
    page.get_by_role("button", name="Create mapping").click()
    expect(page.get_by_text("Saved the mapping profile 'E2E wide layout'")).to_be_visible()

    # ---- long layout: a name that is neither an alias nor a catalog analyte is refused, loudly
    page.goto(f"/analytical/batches/{batch.pk}/import-mapped/")
    bad = LONG.format(lab=one.lab_id, lead="Pb 283.305", when=stamp)
    upload(page, "long.csv", bad, "E2E long layout")
    expect(page.get_by_role("alert").filter(has_text="error")).to_be_visible()
    expect(page.get_by_text("No test 'Pb 283.305'")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^Import \d+ result"))).to_have_count(0)
    assert not ResultRevision.objects.filter(result__batch=batch).exists()  # nothing saved

    # ---- the corrected file previews clean, shows what will be recorded, then commits
    good = LONG.format(lab=one.lab_id, lead="Pb 220.353", when=stamp)
    page.goto(f"/analytical/batches/{batch.pk}/import-mapped/")
    upload(page, "long.csv", good, "E2E long layout")
    expect(page.get_by_text("passed validation. Nothing is saved yet.")).to_be_visible()
    expect(page.get_by_role("cell", name="5.20", exact=True)).to_be_visible()  # as supplied
    expect(page.get_by_role("cell", name="ND", exact=True)).to_be_visible()
    shown_hash = (
        page.locator("div")
        .filter(has_text=re.compile(r"^File SHA-256"))
        .locator("span[title]")
        .last.get_attribute("title")
    )
    assert shown_hash == hashlib.sha256(good.encode()).hexdigest()  # the exact uploaded bytes
    assert ResultRevision.objects.filter(result__batch=batch).count() == 0  # still nothing saved
    page.get_by_role("button", name="Import 2 result(s) as entered").click()
    expect(page.get_by_text("Imported 2 result(s) from long.csv as entered.")).to_be_visible()

    lead = ResultRevision.objects.get(
        result__requested_test__sample=one, result__requested_test__definition__analyte__name="Lead"
    )
    assert lead.supplied_text == "5.20" and lead.value == Decimal("5.20")  # trailing zero kept
    assert lead.source == "import" and lead.source_detail == "long.csv:row 3"
    copper = ResultRevision.objects.get(
        result__requested_test__sample=one,
        result__requested_test__definition__analyte__name="Copper",
    )
    assert copper.kind == "non_detect" and copper.value is None  # ND is not zero
    event = AuditEvent.objects.get(action="batch.results_imported")
    assert event.after["file_sha256"] == shown_hash and event.after["rows"] == 2
    assert event.after["mapping_snapshot"]["mapping"]["analyte_aliases"]["Cu"] == "Copper"
    page.goto(f"/analytical/results/{lead.result_id}/")
    expect(page.get_by_text("5.20 ug/L")).to_be_visible()
    expect(page.get_by_text("long.csv:row 3")).to_be_visible()

    # ---- wide layout: a blank cell imports nothing, and the preview says so
    page.goto(f"/analytical/batches/{batch.pk}/import-mapped/")
    wide = WIDE.format(lab=two.lab_id, when=stamp)
    upload(page, "wide.tsv", wide, "E2E wide layout")
    expect(page.get_by_text("Blank cells (nothing imported)")).to_be_visible()
    expect(page.get_by_role("cell", name="7.50", exact=True)).to_be_visible()
    page.get_by_role("button", name="Import 1 result(s) as entered").click()
    expect(page.get_by_text("Imported 1 result(s) from wide.tsv as entered.")).to_be_visible()
    assert ResultRevision.objects.filter(result__requested_test__sample=two).count() == 1
    only = ResultRevision.objects.get(result__requested_test__sample=two)
    assert only.supplied_text == "7.50" and only.analyte_name == "Lead"
    assert page.console_errors == []
