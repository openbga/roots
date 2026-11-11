"""Mapped tabular import: two layouts, exact decimals, mapped units, duplicate and stale commits.

Fixtures are labelled schema examples written for these tests, not captured instrument output."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from analytical import import_profiles as ip
from analytical import importing
from analytical import services as svc
from analytical.models import ImportProfile, ResultRevision
from core.models import AuditEvent
from receipt.models import RequestedTest

pytestmark = pytest.mark.django_db
FIXTURES = Path(__file__).parent / "fixtures"

LONG_MAPPING = {
    "header_row": 2,
    "date_format": "iso",
    "sample": "Sample Name",
    "analysis_started_at": "Run Time",
    "analyte": "Element",
    "result": "Conc",
    "unit": {"column": "Units"},
    "reporting_limit": {"column": "RL"},
    "dilution_factor": {"column": "DF"},
    "analyte_aliases": {"Pb 220.353": "Lead (synthetic)", "Cu": "Copper (synthetic)"},
}
WIDE_MAPPING = {
    "header_row": 2,
    "date_format": "iso",
    "sample": "ID",
    "analysis_started_at": "Run Time",
    "dilution_factor": {"column": "Dilution"},
    "analyte_columns": [
        {
            "column": "Pb",
            "analyte": "Lead (synthetic)",
            "unit": "mg/L",
            "reporting_limit": "0.0050",
        },
        {
            "column": "Cu",
            "analyte": "Copper (synthetic)",
            "unit": "mg/L",
            "reporting_limit": "0.0100",
        },
    ],
}


def parse(batch, profile, text, **kwargs):
    """The importer takes the uploaded bytes; the tests write the text."""
    return importing.parse_profile_import(batch, profile, text.encode(), **kwargs)


def commit(**kwargs):
    kwargs["data"] = kwargs.pop("text").encode()
    return importing.commit_profile_import(**kwargs)


def analysed(fmt="iso"):
    moment = (timezone.localtime() - timedelta(hours=2)).replace(second=0, microsecond=0)
    return moment.strftime("%m/%d/%Y %I:%M %p") if fmt == "us_12h" else moment.isoformat()


def example(name, fmt="iso"):
    return (FIXTURES / name).read_text().replace("{ANALYSED}", analysed(fmt))


@pytest.fixture
def batch(config, analyst):
    b = svc.create_batch(actor=analyst, method=config["method"], matrix="water")
    tests = RequestedTest.objects.filter(sample__in=config["samples"])
    svc.add_tests_to_batch(actor=analyst, batch_id=b.pk, requested_test_ids=[t.pk for t in tests])
    return b


def make_profile(actor, layout="long", delimiter="comma", mapping=None, name="Example"):
    mapping = mapping or (LONG_MAPPING if layout == "long" else WIDE_MAPPING)
    return ip.create_profile(
        actor=actor, name=name, layout=layout, delimiter=delimiter, mapping=mapping
    )


# ---------------------------------------------------------------------------- long layout


def test_long_layout_maps_aliases_keeps_exact_text_and_records_provenance(config, analyst, batch):
    profile = make_profile(analyst)
    text = example("schema_example_long.csv")
    preview = parse(batch, profile, text, filename="long.csv")
    assert preview.ok and preview.preamble_rows == 1 and preview.unused_columns == ["Flag"]
    assert [r.values["analyte_supplied"] for r in preview.rows] == ["Pb 220.353", "Cu"]
    revisions = commit(
        actor=analyst,
        batch_id=batch.pk,
        profile_id=profile.pk,
        text=text,
        filename="long.csv",
        mapping_hash=preview.mapping_hash,
        state_token=preview.state_token,
    )
    by = {r.analyte_name: r for r in revisions}
    lead = by["Lead (synthetic)"]
    assert lead.supplied_text == "0.0120" and lead.value == Decimal("0.0120")
    assert lead.source == "import" and lead.source_detail == "long.csv:row 3"
    assert by["Copper (synthetic)"].kind == "non_detect" and by["Copper (synthetic)"].value is None
    event = AuditEvent.objects.get(action="batch.results_imported")
    after = event.after
    assert event.actor == analyst and after["import_kind"] == "mapped" and after["rows"] == 2
    assert after["file_sha256"] == importing.file_digest(text.encode()) == preview.file_hash
    assert after["mapping_sha256"] == ip.mapping_hash(profile)
    assert after["mapping_snapshot"]["mapping"]["analyte_aliases"]["Cu"] == "Copper (synthetic)"
    assert after["mapping_snapshot"]["profile_id"] == profile.pk
    assert after["unmapped_columns"] == ["Flag"]


def test_unknown_names_are_errors_and_nothing_is_inferred(config, analyst, batch):
    profile = make_profile(analyst)
    base = example("schema_example_long.csv")
    unknown_analyte = base.replace("Pb 220.353", "Pb 283.305")  # not an alias, not a catalog name
    preview = parse(batch, profile, unknown_analyte)
    assert not preview.ok
    assert "No test 'Pb 283.305' for sample 'VR-DEMO-1'" in " ".join(preview.rows[0].errors)
    renamed = base.replace("Element", "Analyte")  # a mapped header that is not in the file
    missing = parse(batch, profile, renamed)
    assert not missing.ok and "no column named 'Element' (Analyte)" in missing.file_errors[0]
    with pytest.raises(ValidationError):
        commit(
            actor=analyst,
            batch_id=batch.pk,
            profile_id=profile.pk,
            text=unknown_analyte,
            filename="x.csv",
            mapping_hash=ip.mapping_hash(profile),
            state_token="x",
        )
    assert not ResultRevision.objects.exists()


def test_units_are_mapped_checked_and_never_assumed(config, analyst, batch):
    profile = make_profile(analyst)
    base = example("schema_example_long.csv")
    wrong = parse(batch, profile, base.replace("0.0120,mg/L", "0.0120,ug/L"))
    assert "does not match the test unit 'mg/L'" in " ".join(wrong.rows[0].errors)
    blank = parse(batch, profile, base.replace("0.0120,mg/L", "0.0120,"))
    assert "Units are never assumed" in " ".join(blank.rows[0].errors)
    unitless = {k: v for k, v in LONG_MAPPING.items() if k != "unit"}
    with pytest.raises(ValidationError, match="Say where the unit comes from"):
        make_profile(analyst, mapping=unitless, name="No unit")
    fixed = LONG_MAPPING | {"unit": {"fixed": "mg/L"}}
    ok = make_profile(analyst, mapping=fixed, name="Fixed unit")
    assert parse(batch, ok, base).ok


def test_exact_decimals_are_kept_and_lossy_values_are_rejected(config, analyst, batch):
    profile = make_profile(analyst)
    base = example("schema_example_long.csv")
    lossy = parse(batch, profile, base.replace("0.0120", "0.12345678901"))
    assert "more than 10 decimal places" in " ".join(lossy.rows[0].errors)
    comma = parse(batch, profile, base.replace("0.0120", '"0,0120"'))
    assert "not a plain number" in " ".join(comma.rows[0].errors)
    comparison = parse(batch, profile, base.replace("0.0120", "<0.005"))
    assert "Enter ND" in " ".join(comparison.rows[0].errors)
    text = base.replace("0.0120", "0.1000000001")
    revisions = commit(
        actor=analyst,
        batch_id=batch.pk,
        profile_id=profile.pk,
        text=text,
        filename="x.csv",
        mapping_hash=ip.mapping_hash(profile),
        state_token=parse(batch, profile, text).state_token,
    )
    lead = next(r for r in revisions if r.analyte_name.startswith("Lead"))
    assert lead.supplied_text == "0.1000000001" and lead.value == Decimal("0.1000000001")


def test_duplicate_rows_in_one_file_are_rejected(config, analyst, batch):
    profile = make_profile(analyst)
    base = example("schema_example_long.csv")
    doubled = base + base.splitlines()[2] + "\n"
    preview = parse(batch, profile, doubled)
    assert not preview.ok and "Duplicate of row 3" in " ".join(preview.rows[-1].errors)


# ----------------------------------------------------------------------------- wide layout


def test_wide_layout_tab_delimited_lists_blank_cells_and_imports_the_rest(config, analyst, batch):
    profile = make_profile(analyst, layout="wide", delimiter="tab")
    text = example("schema_example_wide.tsv")
    preview = parse(batch, profile, text, filename="wide.tsv")
    assert preview.ok and len(preview.rows) == 3
    assert preview.blanks == [(4, "VR-DEMO-2", "Cu")]
    assert [r.source for r in preview.rows][:1] == ["row 3, column 'Pb'"]
    revisions = commit(
        actor=analyst,
        batch_id=batch.pk,
        profile_id=profile.pk,
        text=text,
        filename="wide.tsv",
        mapping_hash=preview.mapping_hash,
        state_token=preview.state_token,
    )
    assert len(revisions) == 3
    texts = {(r.result.requested_test.sample.lab_id, r.analyte_name[:2]): r for r in revisions}
    assert texts[("VR-DEMO-2", "Le")].supplied_text == "0.0075"
    assert ("VR-DEMO-2", "Co") not in texts  # the blank cell imported nothing
    assert texts[("VR-DEMO-1", "Le")].source_detail == "wide.tsv:row 3, column Pb"
    assert (
        AuditEvent.objects.get(action="batch.results_imported").after["blank_cells_not_imported"]
        == 1
    )


def test_wide_mapping_needs_unit_and_reporting_limit_for_each_column(config, analyst):
    no_unit = WIDE_MAPPING | {
        "analyte_columns": [{"column": "Pb", "analyte": "Lead (synthetic)", "unit": ""}]
    }
    with pytest.raises(ValidationError, match="needs a unit"):
        make_profile(analyst, layout="wide", delimiter="tab", mapping=no_unit, name="A")
    no_limit = {k: v for k, v in WIDE_MAPPING.items() if k != "analyte_columns"} | {
        "analyte_columns": [{"column": "Pb", "analyte": "Lead (synthetic)", "unit": "mg/L"}]
    }
    with pytest.raises(ValidationError, match="reporting limit comes from"):
        make_profile(analyst, layout="wide", delimiter="tab", mapping=no_limit, name="B")
    with pytest.raises(ValidationError, match="not an analyte in the catalog"):
        make_profile(
            analyst,
            layout="wide",
            delimiter="tab",
            mapping=WIDE_MAPPING
            | {
                "analyte_columns": [
                    {"column": "Zn", "analyte": "Zinc", "unit": "mg/L", "reporting_limit": "1"}
                ]
            },
            name="C",
        )


def test_us_date_format_header_row_and_ambiguous_time(config, analyst, batch):
    mapping = WIDE_MAPPING | {"date_format": "us_12h"}
    profile = make_profile(analyst, layout="wide", delimiter="tab", mapping=mapping)
    text = example("schema_example_wide.tsv", fmt="us_12h")
    assert parse(batch, profile, text).ok
    bad = parse(batch, profile, example("schema_example_wide.tsv"))
    assert "does not match the declared date format" in " ".join(bad.rows[0].errors)


# ----------------------------------------------------------------- stale and repeated commits


def test_repeat_commit_cannot_duplicate_and_a_changed_mapping_is_refused(config, analyst, batch):
    profile = make_profile(analyst)
    text = example("schema_example_long.csv")
    token = parse(batch, profile, text).state_token
    args = {
        "actor": analyst,
        "batch_id": batch.pk,
        "profile_id": profile.pk,
        "text": text,
        "state_token": token,
    }
    first_hash = ip.mapping_hash(profile)
    commit(**args, filename="a.csv", mapping_hash=first_hash)
    assert ResultRevision.objects.count() == 2
    with pytest.raises(ValidationError, match="error"):  # the same file was already imported
        commit(**args, filename="a.csv", mapping_hash=first_hash)
    assert ResultRevision.objects.count() == 2
    again = parse(batch, profile, text)
    assert "already imported into this batch" in " ".join(again.file_errors)
    ip.update_profile(
        actor=analyst,
        profile_id=profile.pk,
        name="Example",
        layout="long",
        delimiter="comma",
        mapping=LONG_MAPPING | {"dilution_factor": {"fixed": "1"}},
        is_active=True,
    )
    with pytest.raises(ValidationError, match="mapping profile changed"):
        commit(**args, filename="a.csv", mapping_hash=first_hash)
    profile.refresh_from_db()
    ip.update_profile(
        actor=analyst,
        profile_id=profile.pk,
        name="Example",
        layout="long",
        delimiter="comma",
        mapping=profile.mapping,
        is_active=False,
    )
    with pytest.raises(ValidationError, match="no longer active"):
        commit(**args, filename="a.csv", mapping_hash=ip.mapping_hash(profile))
    assert AuditEvent.objects.filter(action="import_profile.updated").count() == 2


def test_a_result_entered_after_the_preview_blocks_the_commit(config, analyst, batch):
    profile = make_profile(analyst)
    text = example("schema_example_long.csv")
    preview = parse(batch, profile, text)
    assert preview.ok
    result = preview.rows[0].result
    svc.enter_result(
        actor=analyst,
        result_id=result.pk,
        supplied_text="0.0300",
        reporting_limit_applied="0.0050",
        dilution_factor="1",
        analysis_started_at=timezone.now() - timedelta(hours=1),
    )
    with pytest.raises(ValidationError, match="changed after the preview"):
        commit(
            actor=analyst,
            batch_id=batch.pk,
            profile_id=profile.pk,
            text=text,
            filename="a.csv",
            mapping_hash=preview.mapping_hash,
            state_token=preview.state_token,
        )
    assert ResultRevision.objects.count() == 1  # only the manual entry


def test_only_analysts_manage_mappings_and_import(config, analyst, reviewer):
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        make_profile(reviewer)
    profile = make_profile(analyst)
    with pytest.raises(PermissionDenied):
        commit(
            actor=reviewer,
            batch_id=1,
            profile_id=profile.pk,
            text="",
            filename="x",
            mapping_hash=ip.mapping_hash(profile),
            state_token="x",
        )


# --------------------------------------------------------------------------------- HTTP


def test_http_journey_from_mapping_to_preview_to_commit(config, client, analyst, reviewer, batch):
    client.force_login(reviewer)
    assert client.get(reverse("analytical:import-profile-list")).status_code == 200
    assert client.get(reverse("analytical:import-profile-create")).status_code == 403
    client.force_login(analyst)
    form = {
        "name": "Schema example long",
        "layout": "long",
        "delimiter": "comma",
        "header_row": "2",
        "date_format": "iso",
        "sample_column": "Sample Name",
        "analysis_column": "Run Time",
        "analyte_column": "Element",
        "result_column": "Conc",
        "unit_column": "Units",
        "reporting_limit_column": "RL",
        "dilution_column": "DF",
        "analyte_aliases": "Pb 220.353 | Lead (synthetic)\nCu | Copper (synthetic)",
    }
    bad = client.post(reverse("analytical:import-profile-create"), form | {"unit_column": ""})
    assert bad.status_code == 200 and b"Say where the unit comes from" in bad.content
    assert b'value="Schema example long"' in bad.content  # input preserved
    created = client.post(reverse("analytical:import-profile-create"), form)
    assert created.status_code == 302
    profile = ImportProfile.objects.get()
    assert profile.mapping["analyte_aliases"]["Cu"] == "Copper (synthetic)"
    assert b"Schema example long" in client.get(reverse("analytical:import-profile-list")).content
    edit = client.get(reverse("analytical:import-profile-edit", args=[profile.pk]))
    assert b"Pb 220.353 | Lead (synthetic)" in edit.content

    url = reverse("analytical:batch-import-mapped", args=[batch.pk])
    assert client.get(url).status_code == 200
    from django.core.files.uploadedfile import SimpleUploadedFile

    upload = SimpleUploadedFile(
        "run.csv", example("schema_example_long.csv").encode(), content_type="text/csv"
    )
    preview = client.post(url, {"step": "preview", "profile": profile.pk, "file": upload})
    page = preview.content.decode()
    assert preview.status_code == 200 and "passed validation" in page
    assert "New result" in page and "(file: Pb 220.353)" in page and "File SHA-256" in page
    assert not ResultRevision.objects.exists()  # preview writes nothing
    ctx = preview.context
    commit = ctx["commit_form"].initial | {"step": "commit"}
    done = client.post(url, commit)
    assert done.status_code == 302 and done.url == reverse(
        "analytical:batch-detail", args=[batch.pk]
    )
    assert ResultRevision.objects.filter(source="import").count() == 2
    again = client.post(url, commit)  # a repeated submit is refused, nothing duplicated
    assert again.status_code == 200 and ResultRevision.objects.count() == 2
    assert b"Nothing was imported" in again.content
    assert b"already imported into this batch" in again.content


@pytest.mark.parametrize(
    "variant",
    ["bom_lf", "crlf"],
)
def test_preview_digest_provenance_and_repeat_detection_match_the_committed_bytes(
    config, client, analyst, batch, variant
):
    """The page the browser receives carries the uploaded bytes unchanged, so the digest shown
    in the preview is the one stored, and uploading the same file again is recognised."""
    import base64
    import hashlib
    import html
    import re

    from django.core.files.uploadedfile import SimpleUploadedFile

    profile = make_profile(analyst)
    text = example("schema_example_long.csv")  # LF-terminated, ends with a newline
    raw = (
        b"\xef\xbb\xbf" + text.encode()
        if variant == "bom_lf"
        else text.replace("\n", "\r\n").encode()
    )
    digest = hashlib.sha256(raw).hexdigest()
    client.force_login(analyst)
    url = reverse("analytical:batch-import-mapped", args=[batch.pk])

    def upload():
        return client.post(
            url,
            {
                "step": "preview",
                "profile": profile.pk,
                "file": SimpleUploadedFile("run.csv", raw, content_type="text/csv"),
            },
        )

    preview = upload()
    page = preview.content.decode()
    assert preview.status_code == 200 and "passed validation" in page
    assert digest in page and "File SHA-256 (uploaded bytes)" in page
    assert "Not stored by this application" in page and "Keep the source file" in page
    # Post back exactly the hidden fields the browser would hold.
    hidden = {}
    for tag in re.findall(r"<input[^>]*type=\"hidden\"[^>]*>", page):
        name = re.search(r'name="([^"]+)"', tag).group(1)
        value = re.search(r'value="([^"]*)"', tag)
        hidden[name] = html.unescape(value.group(1)) if value else ""
    assert base64.b64decode(hidden["payload"], validate=True) == raw
    assert not re.search(r"\s", hidden["payload"])  # nothing for a browser to trim or normalise
    done = client.post(url, {**hidden, "step": "commit"})
    assert done.status_code == 302 and ResultRevision.objects.filter(source="import").count() == 2
    after = AuditEvent.objects.get(action="batch.results_imported").after
    assert after["file_sha256"] == digest and after["file_bytes"] == len(raw)
    assert after["file_bom_removed"] is (variant == "bom_lf")
    lead = ResultRevision.objects.get(analyte_name__startswith="Lead")
    assert lead.supplied_text == "0.0120"  # numeric text exact through the round trip
    repeat = upload()
    assert b"already imported into this batch" in repeat.content
    assert b'name="step" value="commit"' not in repeat.content


def test_commit_rejects_a_damaged_or_oversized_payload_without_importing(
    config, client, analyst, batch
):
    import base64

    profile = make_profile(analyst)
    client.force_login(analyst)
    url = reverse("analytical:batch-import-mapped", args=[batch.pk])
    fields = {
        "step": "commit",
        "filename": "x.csv",
        "profile_id": profile.pk,
        "mapping_hash": ip.mapping_hash(profile),
        "state_token": "x",
    }
    damaged = client.post(url, {**fields, "payload": "not base64 !!"})
    assert damaged.status_code == 200 and b"could not be read back" in damaged.content
    big = base64.b64encode(b"a" * (importing.MAX_BYTES + 1)).decode()
    oversized = client.post(url, {**fields, "payload": big})
    assert oversized.status_code == 200 and b"is larger than" in oversized.content
    assert not ResultRevision.objects.exists()


def test_mapping_form_labels_fit_and_the_note_has_room(config, client, analyst):
    client.force_login(analyst)
    page = client.get(reverse("analytical:import-profile-create")).content.decode()
    assert ">Long (one result per row)</option>" in page
    assert ">Wide (one row per sample)</option>" in page
    assert ">MM/DD/YYYY HH:MM AM/PM</option>" in page
    assert 'class="vr-form-note mt-3 mb-0"><strong>Long:</strong>' in page
    assert "not the uploaded file: keep your source file" in page


def test_stale_profile_edit_keeps_newer_mapping(client, config, analyst):
    from core.models import AuditEvent

    client.force_login(analyst)
    profile = make_profile(analyst)
    url = reverse("analytical:import-profile-edit", args=[profile.pk])
    form = client.get(url).context["form"]
    data = {f.name: f.value() if f.value() is not None else "" for f in form}
    data["notes"] = "Pending mapping notes"
    type(profile).objects.filter(pk=profile.pk).update(notes="Newer mapping notes")
    count = AuditEvent.objects.count()
    response = client.post(url, data)
    assert response.status_code == 200
    assert "changed after you opened" in str(response.context["form"].errors)
    assert response.context["form"]["notes"].value() == "Pending mapping notes"
    profile.refresh_from_db()
    assert profile.notes == "Newer mapping notes"
    assert AuditEvent.objects.count() == count
    data["original_state"] = client.get(url).context["form"]["original_state"].value()
    assert client.post(url, data).status_code == 302
    profile.refresh_from_db()
    assert profile.notes == "Pending mapping notes"
