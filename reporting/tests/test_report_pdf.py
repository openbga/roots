"""The issued-report layout (reporting/templates/reporting/pdf/report.html): every regulated element
of a snapshot prints, amendment changes print their old value struck through (structured entries)
or as stored (sentences of versions issued before), and the reading aids only repeat recorded
facts. Real-render checks follow test_release.test_real_pdf_render's environment rules."""

import re
import zlib
from copy import deepcopy
from html import unescape

import pytest

from analytical import services as analytical
from analytical.models import Result
from reporting import pdf as report_pdf
from reporting.content import assemble, change_text, describe_changes
from reporting.pdf import layout, render_html

from .conftest import texts
from .test_lab_identity import require_weasyprint
from .test_release import correct, release
from .test_subcontract_rendering import released_snapshot


def text_of(html):
    """Visible text of rendered markup, whitespace collapsed."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def amend(world, people):
    """v1 of report_a, then v2 after a corrected result (0.050 -> 0.048 mg/L)."""
    v1 = release(world["report_a"], people["approver"])
    revision = correct(world["sample_a"], people, "0.048")
    analytical.submit_result(actor=people["analyst"], revision_id=revision.pk)
    analytical.approve_result(actor=people["reviewer"], revision_id=revision.pk)
    return v1, release(world["report_a"], people["approver2"], reason="Corrected lead result")


def test_new_amendment_stores_structured_change_and_strikes_the_old_value(world, people, client):
    _, v2 = amend(world, people)
    (change,) = [c for c in v2.changes if c.get("field") == "result"]
    assert change == {
        "text": change["text"],
        "subject": "VR-A1 Lead (synthetic)",
        "field": "result",
        "old": "0.050 mg/L, RL 0.01 (revision 1)",
        "new": "0.048 mg/L, RL 0.01 (revision 2)",
    }
    assert change["text"] == f"VR-A1 Lead (synthetic): {change['old']} → {change['new']}."
    assert v2.file.snapshot["release"]["changes"] == v2.changes
    html = render_html(v2.file.snapshot)
    assert (
        "<li>VR-A1 Lead (synthetic): <s>0.050 mg/L, RL 0.01 (revision 1)</s> → "
        "0.048 mg/L, RL 0.01 (revision 2)" in html
    )
    # The staff report record prints the same entry through the shared include.
    client.force_login(people["pm"])
    page = client.get(f"/reports/{world['report_a'].pk}/").content.decode()
    assert "<s>0.050 mg/L, RL 0.01 (revision 1)</s>" in page


def test_identity_value_change_keeps_old_and_new_text(world):
    old = assemble(world["report_a"]).content
    new = deepcopy(old)
    new["client"]["name"] = "Example Utilities (renamed)"
    new["report"]["narrative"] = "Changed narrative"
    assert describe_changes(old, new) == [
        {"text": "Case narrative changed."},
        {
            "text": "Client name changed.",
            "subject": "Client name",
            "field": "client.name",
            "old": "Example Utilities",
            "new": "Example Utilities (renamed)",
        },
    ]
    # A value recorded on one side only has no old text to strike: the sentence prints.
    new["client"]["contact_name"] = "Synthetic Contact"
    assert {"text": "Client contact changed."} in describe_changes(old, new)


def test_legacy_string_changes_print_as_stored(world, people):
    v1, v2 = amend(world, people)
    legacy = deepcopy(v2.file.snapshot)
    sentences = texts(legacy["release"]["changes"])
    legacy["release"]["changes"] = sentences  # how versions issued before stored them
    html = render_html(legacy)
    for sentence in sentences:
        assert f"<li>{sentence}\n</li>" in html or f"<li>{sentence}</li>" in html
    assert "<s>" not in html
    assert change_text(sentences[0]) == sentences[0]
    # The stored version is untouched and still renders its structured entries.
    assert v2.file.snapshot["release"]["changes"] != sentences
    assert "<s>0.050 mg/L" in render_html(v2.file.snapshot)
    # Issued versions are stored: the earlier one keeps its bytes and snapshot.
    v1.refresh_from_db()
    assert v1.file.snapshot["release"]["changes"] == []


QUALIFIER = {"code": "J", "description": "Estimated value (synthetic test)."}


def full_snapshot(world, people, base=None):
    """A released snapshot with every optional element the layout prints filled in (synthetic).
    `base` is a released_snapshot (with a subcontracted result) to fill; released here if absent."""
    snapshot = deepcopy(base or released_snapshot(world, people))
    c = snapshot["content"]
    c["lab"].update(
        address=["1 Synthetic Way", "Testville, NY 00000"],
        phone="555-0100",
        email="lab@example.invalid",
        website="https://lab.example.invalid",
        logo={"sha256": "a" * 64, "content_type": "image/png", "width": 240, "height": 80},
        certificates=[
            {
                "authority": "Example Authority",
                "program": "Synthetic water program",
                "number": "EX-CERT-1",
                "valid_from": "2026-01-01",
                "valid_until": "2027-12-31",
            }
        ],
        certificate_note="Synthetic certificate statement.",
    )
    c["report"]["narrative"] = "Synthetic narrative line."
    c["client"].update(contact_name="Synthetic Contact", address=["9 Client Road"])
    c["project"]["program"] = "Synthetic permit P-1"
    s = c["samples"][0]
    s.update(
        source="Surface water",
        sample_type="Field duplicate",
        collection_started_at="2026-11-01T06:00:00-05:00",
        discrepancy="Synthetic discrepancy",
        disposition_note="Synthetic disposition note",
        coolant="Wet ice",
        temperature_instrument="Synthetic IR-1",
    )
    r = s["results"][0]
    r.update(
        revision=2,
        correction_reason="Synthetic correction reason",
        comments="Synthetic comment",
        qualifiers=["J"],
        percent_solids="87.5",
        solids_reference="Synthetic solids reference",
        holding_status="Exceeded",
        holding=[
            {
                "name": "Synthetic hold",
                "status": "exceeded",
                "deadline": "2026-11-02T06:00:00-05:00",
                "end_at": None,
            }
        ],
    )
    c["qualifiers"] = [QUALIFIER]
    batch = next(b for b in c["batches"] if b["number"] == r["batch"])
    qc = batch["qc"][r["analyte"]]
    qc["cancelled_aliquots"] = 1
    qc["measurements"] = [
        {
            "qc_sample": "LCS-SYN",
            "qc_type": "lcs",
            "observed_text": "0.61",
            "unit": "mg/L",
            "withheld": False,
            "evaluation": {
                "status": "fail",
                "measures": [{"detail": "recovery % 122 is outside limits"}],
            },
        },
        {
            "qc_sample": "Sample not in this report",
            "qc_type": "matrix_spike",
            "observed_text": None,
            "unit": "mg/L",
            "withheld": True,
            "evaluation": {"status": "pass", "measures": [{"detail": "recovery: pass"}]},
        },
    ]
    qc["issues"] = [
        {
            "key": "m:1:recovery",
            "code": "failed",
            "qc_type": "lcs",
            "message": "LCS-SYN recovery outside limits.",
            "disposition": "accept_qualified",
            "note": "Synthetic QC note",
        }
    ]
    return snapshot


# Every regulated element of the issued report, with the value the full snapshot gives it.
# Each entry is (element, text that must print). Keep in step with the layout's comment.
def checklist(snapshot):
    c, release_ = snapshot["content"], snapshot["release"]
    s = c["samples"][0]
    return [
        ("Lab name (header and letterhead)", c["lab"]["name"]),
        ("Lab address", "1 Synthetic Way"),
        ("Lab phone", "Phone 555-0100"),
        ("Lab email", "lab@example.invalid"),
        ("Lab website", "https://lab.example.invalid"),
        ("Report number", c["report"]["number"]),
        ("Version", f"Version {release_['version']}"),
        ("Title", c["report"]["title"]),
        ("Client", c["client"]["name"]),
        ("Client contact", "Attn: Synthetic Contact"),
        ("Client address", "9 Client Road"),
        ("Project", c["project"]["name"]),
        ("Program / permit", "Program / permit: Synthetic permit P-1"),
        ("Released by", release_["released_by"]),
        ("Release time", release_["released_at"][:10]),
        ("Time zone statement", f"Laboratory local time ({c['lab']['timezone']}), UTC offset"),
        ("Content SHA-256", snapshot["content_sha256"]),
        ("Synthetic notice", "Synthetic demonstration data. Not a laboratory result."),
        ("Case narrative", "Synthetic narrative line."),
        ("Lab ID", s["lab_id"]),
        ("Client sample ID", s["client_sample_id"]),
        ("Chain of custody", s["coc_reference"]),
        ("Matrix and source", f"{s['matrix']}, surface water"),
        ("Collection type", s["collection_type"]),
        ("Sample type", "Field duplicate"),
        ("Collection location", s["collection_location"]),
        ("Composite start", "Composite start"),
        ("Composite end", "Composite end"),
        ("Collector", s["collector"]),
        ("Received by", s["received_by"]),
        ("Cooler temperature", f"{s['cooler_temperature']} {s['cooler_temperature_unit']} (as"),
        ("Coolant", "coolant on arrival: wet ice"),
        ("Thermometer", "Synthetic IR-1"),
        ("Custody seal", s["seal_condition"]),
        ("Preservation check", s["preservation_check"]),
        ("Receipt discrepancy", "Synthetic discrepancy"),
        ("Disposition note", "Synthetic disposition note"),
        ("Receipt disposition", s["disposition"]),
        ("Analyte", s["results"][0]["analyte"]),
        ("CAS number", f"CAS {s['results'][0]['cas_number']}"),
        ("Result", s["results"][0]["reported"]),
        ("Unit", s["results"][0]["unit"]),
        ("Qualifier and meaning", "J : Estimated value (synthetic test)."),
        (
            "Method and version",
            f"{s['results'][0]['method_code']} ({s['results'][0]['method_version']})",
        ),
        ("Batch", f"Batch {s['results'][0]['batch']}"),
        ("Analyst", f"Analyst {s['results'][0]['analyst']}"),
        ("Reviewer", f"Reviewed by {s['results'][0]['reviewed_by']}"),
        ("Holding status", "Exceeded"),
        ("Holding detail", "Synthetic hold: exceeded, deadline 2026-11-02"),
        ("Revision note", "Revision 2: Synthetic correction reason"),
        ("Percent solids", "87.5% solids (Synthetic solids reference)"),
        ("Comment", "Comment: Synthetic comment"),
        ("Receipt preservation screening", "Preservation"),
        ("Preparation note", "Preparation"),
        ("Subcontracted result", "Subcontracted"),
        ("External laboratory", "Synthetic Partner Lab"),
        ("External report", "SPL-REPORT-0001"),
        ("External qualifiers", "Qualifiers as reported by Synthetic Partner Lab: J: estimated"),
        ("External holding", "Not evaluated by this laboratory"),
        ("QC batch", f"Analytical batch {s['results'][0]['batch']}"),
        ("QC sample", "LCS-SYN"),
        ("QC type", "Laboratory control sample"),
        ("QC observed", "0.61 mg/L"),
        ("QC evaluation", "Fail"),
        ("QC detail", "Recovery % 122 is outside limits"),
        ("Withheld QC", "Not shown"),
        ("Cancelled aliquots", "includes 1 cancelled aliquot"),
        ("QC issue", "QC issue : LCS-SYN recovery outside limits. Affects"),
        ("QC disposition and note", "Disposition: accept qualified (Synthetic QC note)"),
        ("Method title", c["methods"][0]["title"]),
        ("Qualifier legend", "Estimated value (synthetic test)."),
        ("Abbreviations", "ND: not detected at or above the reporting limit (RL)."),
        ("Certificate authority", "Example Authority"),
        ("Certificate scope", "Synthetic water program"),
        ("Certificate ID", "EX-CERT-1"),
        ("Certificate validity", "From 2026-01-01 to 2027-12-31"),
        ("Certificate statement", "Synthetic certificate statement."),
        ("Release statement", "Each result was technically reviewed by the reviewer named"),
    ]


def test_every_regulated_element_prints(world, people):
    snapshot = full_snapshot(world, people)
    html = render_html(snapshot)
    text = text_of(html)
    missing = [name for name, needle in checklist(snapshot) if needle not in text]
    assert not missing, missing
    assert f'src="lab-logo:{"a" * 64}"' in html


def test_amendment_and_preview_marks_print(world, people):
    _, v2 = amend(world, people)
    text = text_of(render_html(v2.file.snapshot))
    for needle in (
        "Amended report",
        "This version 2 replaces version 1 issued",
        "The earlier version remains on record.",
        "Reason: Corrected lead result",
        "Version 2 · Amended",
    ):
        assert needle in text
    preview = text_of(render_html({"content": v2.file.snapshot["content"]}, preview=True))
    for needle in ("PREVIEW · NOT ISSUED", "Preview, not issued", "Preview only."):
        assert needle in preview
    assert "Amended report" not in preview


def test_attention_flags_repeat_recorded_facts_only(world, people):
    base = released_snapshot(world, people)
    snapshot = full_snapshot(world, people, base)
    view = layout(snapshot["content"])
    (flagged,) = view["attention"]
    assert flagged["qualifiers"] == [("J", QUALIFIER["description"])]
    assert flagged["holding_flag"] == "Exceeded"
    assert [i["message"] for i in flagged["qc_issues"]] == ["LCS-SYN recovery outside limits."]
    sample = view["samples"][0]
    assert sample["qualifier_flags"] == ["J"] and sample["holding_flags"] == ["Exceeded"]
    text = text_of(render_html(snapshot))
    assert "Needs attention: Qualified J Holding exceeded QC exception" in text
    assert "VR-A1 (" in text and "qualifier J; holding time exceeded; QC exception in batch" in text
    # Nothing flagged: the box says so and no sample carries a "Needs attention" line.
    plain = render_html(base)
    assert "No result carries a qualifier, a holding-time flag or a QC issue." in text_of(plain)
    assert "Needs attention" not in plain
    # A subcontracted result's "Not evaluated by this laboratory" is not a flag.
    assert all(e["r"].get("source") != "external" for e in view["attention"])


def test_receipt_note_prints_once_when_shared_and_per_result_when_not(world, people):
    snapshot = released_snapshot(world, people)
    sample = snapshot["content"]["samples"][0]
    internal = sample["results"][0]
    sample["results"].append({**deepcopy(internal), "analyte": "Copper (synthetic)"})
    html = render_html(snapshot)
    # The subcontracted row has no preparation start, so its note differs: one per result.
    assert html.count('<tr class="note">') == 3 and "for every result of this sample" not in html
    sample["results"] = [internal, {**deepcopy(internal), "analyte": "Copper (synthetic)"}]
    html = render_html(snapshot)
    assert '<tr class="note">' not in html
    assert html.count("for every result of this sample") == 1
    assert html.count("Receipt preservation screening") + html.count("Preservation screening") == 1


def test_qc_prints_one_table_per_batch(world, people):
    snapshot = released_snapshot(world, people)
    c = snapshot["content"]
    batch = c["batches"][0]
    qc = next(iter(batch["qc"].values()))
    batch["qc"]["Copper (synthetic)"] = deepcopy(qc)
    html = render_html(snapshot)
    qc_part = html[html.index("Batch quality control summary") :]
    assert qc_part.count('<table class="grid qc-table">') == len(c["batches"])
    assert qc_part.count('class="qc-group"') == 2


STREAM = rb"stream\r?\n(.*?)\r?\nendstream"


def inflate(data):
    """A Flate stream's bytes (font dictionaries may sit in compressed object streams)."""
    try:
        return zlib.decompress(data)
    except zlib.error:
        return b""


def test_real_pdf_embeds_the_vendored_overpass(world, people):
    require_weasyprint()
    snapshot = full_snapshot(world, people)
    del snapshot["content"]["lab"]["logo"]  # not stored in this test
    pdf = report_pdf.render_pdf(snapshot)
    assert pdf.startswith(b"%PDF")
    objects = pdf + b"".join(inflate(m) for m in re.findall(STREAM, pdf, re.S))
    fonts = set(re.findall(rb"/BaseFont\s*/[A-Z]{6}\+([\w-]+)", objects))
    assert {b"Overpass", b"Overpass-Semi-Bold", b"Overpass-Bold"} <= fonts, fonts


def test_missing_font_fails_the_render(world, people, monkeypatch, tmp_path):
    monkeypatch.setattr(report_pdf, "FONT_DIR", tmp_path)
    with pytest.raises(report_pdf.ReportRenderError, match="fonts"):
        report_pdf.render_pdf(released_snapshot(world, people))


def test_issued_pdf_is_served_as_stored_not_rerendered(world, people, client, monkeypatch):
    version = release(world["report_a"], people["approver"])
    stored = bytes(version.file.pdf)

    def refuse(*_args, **_kwargs):
        raise AssertionError("an issued version must not be rendered again")

    monkeypatch.setattr(report_pdf, "render_pdf", refuse)
    monkeypatch.setattr("reporting.views.render_pdf", refuse)
    client.force_login(people["custodian"])
    response = client.get(f"/reports/{world['report_a'].pk}/v1.pdf")
    assert response.content == stored
    client.force_login(world["user_a"])
    assert client.get(f"/portal/reports/{world['report_a'].pk}/v1.pdf").content == stored
    assert Result.objects.exists()


def page_texts(document):
    """Visible text of each laid-out page, running header and footer included."""

    def walk(box):
        yield getattr(box, "text", "")
        for child in getattr(box, "all_children", lambda: [])():
            yield from walk(child)

    return [re.sub(r"\s+", " ", " ".join(walk(page._page_box))) for page in document.pages]


def test_page_breaks_keep_each_result_whole_and_identity_on_every_page(world, people):
    """A result's value row, its exception lines and its provenance line never split; a
    one-result table moves whole; every page names the report number and version."""
    require_weasyprint()
    snapshot = released_snapshot(world, people)
    c = snapshot["content"]
    first = c["samples"][0]
    internal = first["results"][0]
    first["results"] = [
        {
            **deepcopy(internal),
            "analyte": f"Analyte {n:02d}",
            "comments": f"Provenance {n:02d}",
            "qualifiers": ["J"] if n % 7 == 0 else [],
        }
        for n in range(40)
    ]
    c["qualifiers"] = [QUALIFIER]
    lone = deepcopy(first)
    lone.update(lab_id="VR-LONE", client_sample_id="LONE-1")
    lone["results"] = [{**deepcopy(internal), "analyte": "Lone analyte", "comments": "Lone note"}]
    c["samples"].append(lone)
    pages = page_texts(report_pdf.render_document(snapshot))
    assert len(pages) > 2
    for n in range(40):
        # The last mention is the result row (a flagged result is also listed At a glance).
        value_page = max(i for i, t in enumerate(pages) if f"Analyte {n:02d} " in t)
        assert f"Provenance {n:02d}" in pages[value_page], n
        if n % 7 == 0:
            assert "Estimated value (synthetic test)." in pages[value_page], n
    lone_page = next(i for i, t in enumerate(pages) if "Lone analyte" in t)
    assert "Lone note" in pages[lone_page]
    assert "HOLDING TIME" in pages[lone_page].split("Lone analyte")[0]  # header came along
    number = f"{c['report']['number']} · Version {snapshot['release']['version']}"
    assert all(number in t and snapshot["content_sha256"] in t for t in pages)


def test_a_qc_issue_flags_only_the_results_of_its_analyte(world, people):
    """One failing Lead QC measurement in a batch that also holds Copper and Zinc results: only
    the Lead results are flagged and named (the batch-and-test relation release uses)."""
    snapshot = released_snapshot(world, people)
    c = snapshot["content"]
    sample = c["samples"][0]
    lead = sample["results"][0]
    batch = next(b for b in c["batches"] if b["number"] == lead["batch"])
    clean = deepcopy(batch["qc"][lead["analyte"]])
    for offset, name in enumerate(("Copper (synthetic)", "Zinc (synthetic)"), start=1):
        test = lead["test_definition_id"] + 1000 * offset  # a different test in the same batch
        sample["results"].append({**deepcopy(lead), "analyte": name, "test_definition_id": test})
        batch["qc"][name] = {**deepcopy(clean), "test_definition_id": test}
    batch["qc"][lead["analyte"]]["issues"] = [
        {
            "key": "m:1:x",
            "code": "failed",
            "qc_type": "lcs",
            "message": "LCS-1 Lead (synthetic): recovery outside limits.",
            "disposition": "accept_qualified",
            "note": "",
        }
    ]
    view = layout(c)
    assert [(a["sample"]["lab_id"], a["r"]["analyte"]) for a in view["attention"]] == [
        ("VR-A1", "Lead (synthetic)")
    ]
    groups = {g["analyte"]: g for g in view["batches"][0]["groups"]}
    assert list(groups) == sorted(groups)  # the results tables' order
    text = text_of(render_html(snapshot))
    assert "Affects Lead (synthetic) in VR-A1." in text
    assert text.count("QC exception : batch") == 1
    assert "Affects Copper" not in text and "Affects Zinc" not in text


def accepted_lcs_failure(world, people):
    """A failing LCS for the world's test in sample_a's batch, accepted with qualification."""
    from datetime import timedelta
    from decimal import Decimal

    from django.utils import timezone

    from analytical.models import QCLimit, QCMeasurement

    from .conftest import accept_qc

    batch = Result.objects.get(requested_test__sample=world["sample_a"]).batch
    QCLimit.objects.create(
        definition=world["definition"],
        qc_type="lcs",
        measure="recovery",
        lower=Decimal("80"),
        upper=Decimal("120"),
    )
    lcs = analytical.add_qc_sample(
        actor=people["analyst"], batch_id=batch.pk, qc_type="lcs", label="LCS-1"
    )
    analytical.record_qc_measurement(
        actor=people["analyst"],
        qc_sample_id=lcs.pk,
        definition_id=world["definition"].pk,
        observed_text="0.50",
        reporting_limit_applied="0.010",
        analysis_started_at=timezone.now() - timedelta(minutes=30),
        spike_value="1.00",
    )
    measurement = QCMeasurement.objects.get(qc_sample=lcs, is_current=True)
    accept_qc(people["reviewer"], measurement, "LCS low; results accepted with qualification.")


def test_qc_flag_follows_the_test_when_the_analyte_is_renamed_after_entry(world, people):
    """The result keeps the analyte name it was entered under; its batch QC is keyed by the
    current name. The recorded test definition still links them, so the flag is not lost."""
    from catalog.models import Analyte

    accepted_lcs_failure(world, people)
    Analyte.objects.filter(pk=world["definition"].analyte_id).update(name="Lead (renamed)")
    version = release(world["report_a"], people["approver"])
    c = version.file.snapshot["content"]
    row = c["samples"][0]["results"][0]
    assert row["analyte"] == "Lead (synthetic)" and "Lead (renamed)" in c["batches"][0]["qc"]
    assert row["test_definition_id"] == world["definition"].pk
    (flagged,) = layout(c)["attention"]
    assert flagged["r"] is row and flagged["qc_issues"]
    text = text_of(render_html(version.file.snapshot))
    assert "Affects Lead (renamed) in VR-A1." in text and "QC exception : batch" in text


def test_snapshots_without_the_test_reference_match_qc_by_name(world, people):
    accepted_lcs_failure(world, people)
    version = release(world["report_a"], people["approver"])
    legacy = deepcopy(version.file.snapshot["content"])
    for s in legacy["samples"]:
        for r in s["results"]:
            del r["test_definition_id"]
    for b in legacy["batches"]:
        for qc in b["qc"].values():
            del qc["test_definition_id"]
    assert len(layout(legacy)["attention"]) == 1
    # The reference is not printed and never reads as a change against an older snapshot.
    assert describe_changes(legacy, version.file.snapshot["content"]) == []
