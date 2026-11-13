"""Spreadsheet import: roles, all-or-nothing commit, stale previews, exact decimals, update
semantics, identity locks, references and audit."""

import hashlib
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from analytical.models import HoldingTimeRule, QCLimit
from catalog import services as catalog_services
from catalog.models import Analyte, Method, TestDefinition
from core.models import AuditEvent
from dataimport import importing
from receipt.models import Client, Project

pytestmark = pytest.mark.django_db

T = importing.BY_SLUG


def csv(*lines):
    return ("\r\n".join(lines) + "\r\n").encode()


def preview(kind, user, data, name="file.csv"):
    return importing.preview_file(T[kind], actor=user, data=data, filename=name)


def commit(kind, user, data, name="file.csv", reason="", token=None):
    if token is None:
        token = preview(kind, user, data, name).state_token
    return importing.commit_file(
        T[kind], actor=user, data=data, filename=name, reason=reason, state_token=token
    )


@pytest.fixture
def catalog(admin_user):
    method = Method.objects.create(code="EPA 200.8", version="5.4", title="Metals by ICP-MS")
    lead = Analyte.objects.create(name="Lead", cas_number="7439-92-1")
    test = TestDefinition.objects.create(method=method, analyte=lead, matrix="water", unit="mg/L")
    return method, lead, test


# ------------------------------------------------------------------------------ authorization


@pytest.mark.parametrize("kind", list(T))
def test_roles_per_record_type_on_every_endpoint(client, make_user, kind):
    receipt = T[kind].roles == ("project_manager",)
    allowed = (
        make_user(f"pm-{kind}", "project_manager") if receipt else make_user(f"a-{kind}", "admin")
    )
    refused = [make_user(f"an-{kind}", "analyst"), make_user(f"rv-{kind}", "reviewer")]
    if not receipt:
        refused.append(make_user(f"pm-{kind}", "project_manager"))
    customer = Client.objects.create(name=f"Example Utilities {kind}")
    refused.append(make_user(f"portal-{kind}", client=customer))
    upload = reverse("dataimport:upload", args=[kind])
    template = reverse("dataimport:template", args=[kind])
    data = importing.template_bytes(T[kind])
    for user in refused:
        client.force_login(user)
        assert client.get(upload).status_code == 403, (kind, user)
        assert client.get(template).status_code == 403
        posted = client.post(upload, {"step": "preview", "file": SimpleUploadedFile("x.csv", data)})
        assert posted.status_code == 403
        assert client.post(upload, {"step": "commit"}).status_code == 403
        with pytest.raises(PermissionDenied):
            preview(kind, user, data)
    client.force_login(allowed)
    assert client.get(upload).status_code == 200
    assert (
        client.get(template)
        .content.lstrip("﻿".encode())
        .startswith(T[kind].columns[0].name.encode())
    )


def test_index_lists_only_importable_types_and_refuses_others(client, make_user, manager):
    client.force_login(manager)
    page = client.get(reverse("dataimport:index"))
    assert [item["spec"].slug for item in page.context["types"]] == ["clients", "projects"]
    client.force_login(make_user("bench", "analyst"))
    assert client.get(reverse("dataimport:index")).status_code == 403
    customer = Client.objects.create(name="Example Utilities")
    client.force_login(make_user("portal", "admin", client=customer))
    assert client.get(reverse("dataimport:index")).status_code == 403
    assert client.get(reverse("dataimport:upload", args=["clients"])).status_code == 403


def test_project_manager_cannot_commit_a_catalog_file(manager):
    data = csv("name", "Lead")
    with pytest.raises(PermissionDenied):
        importing.commit_file(
            T["analytes"], actor=manager, data=data, filename="a.csv", reason="", state_token=""
        )
    assert not Analyte.objects.exists()


# --------------------------------------------------------------------- preview and commit


def test_preview_writes_nothing_and_commit_writes_rows_with_audit(admin_user):
    data = csv("name,cas_number,group", "Lead,7439-92-1,metals", "Nitrate,,nutrients")
    shown = preview("analytes", admin_user, data, "analytes.csv")
    assert shown.ok and shown.counts == {"create": 2, "update": 0, "unchanged": 0, "error": 0}
    assert not Analyte.objects.exists() and not AuditEvent.objects.exists()
    commit("analytes", admin_user, data, "analytes.csv", token=shown.state_token)
    digest = hashlib.sha256(data).hexdigest()
    assert set(Analyte.objects.values_list("name", flat=True)) == {"Lead", "Nitrate"}
    created = AuditEvent.objects.filter(action="catalog.analyte.created")
    assert created.count() == 2
    assert all(e.reason == f"Created by import of analytes.csv, SHA-256 {digest}" for e in created)
    summary = AuditEvent.objects.get(action=importing.SUMMARY_ACTION)
    assert summary.actor == admin_user and summary.object_id == digest
    assert summary.after | {} == {
        "record_type": "analytes",
        "filename": "analytes.csv",
        "file_sha256": digest,
        "file_bytes": len(data),
        "rows": 2,
        "created": 2,
        "updated": 0,
        "unchanged": 0,
    }


def test_a_late_service_error_rolls_back_every_row(admin_user):
    # The CAS check digit is the service's rule: reading the file cannot see it.
    data = csv("name,cas_number", "Lead,7439-92-1", "Zinc,7440-66-0")
    token = importing.parse(T["analytes"], data, "a.csv").state_token
    with pytest.raises(ValidationError, match="Nothing was imported"):
        commit("analytes", admin_user, data, "a.csv", token=token)
    assert not Analyte.objects.exists() and not AuditEvent.objects.exists()
    rows = preview("analytes", admin_user, data).rows
    assert rows[0].ok and rows[1].errors == ["cas_number: This CAS number's check digit is wrong."]


def test_stale_preview_is_refused_and_nothing_changes(admin_user, catalog):
    method, _, _ = catalog
    data = csv("method_code,method_version,title", "EPA 200.8,5.4,Metals by ICP-MS (rev)")
    shown = preview("methods", admin_user, data)
    assert shown.ok and shown.rows[0].action == "update"
    method.source = "Changed elsewhere"
    catalog_services.save_method(actor=admin_user, instance=method, reason="Concurrent edit")
    with pytest.raises(ValidationError, match="different from the preview"):
        commit("methods", admin_user, data, reason="Retitle", token=shown.state_token)
    method.refresh_from_db()
    assert method.title == "Metals by ICP-MS"


def test_stale_commit_through_the_page_shows_a_fresh_preview(client, admin_user, catalog):
    client.force_login(admin_user)
    url = reverse("dataimport:upload", args=["methods"])
    data = csv("method_code,method_version,title", "EPA 200.8,5.4,New title")
    page = client.post(url, {"step": "preview", "file": SimpleUploadedFile("m.csv", data)})
    form = page.context["commit_form"]
    posted = {name: form[name].value() for name in ("filename", "payload", "state_token")}
    catalog[0].source = "Changed elsewhere"
    catalog_services.save_method(actor=admin_user, instance=catalog[0], reason="Concurrent")
    again = client.post(url, {"step": "commit", "reason": "Retitle", **posted})
    assert again.status_code == 200
    assert again.context["preview"].ok
    assert again.context["commit_form"]["state_token"].value() != posted["state_token"]
    assert again.context["commit_form"]["reason"].value() == "Retitle"
    assert b"different from the preview" in again.content
    final = client.post(
        url,
        {
            "step": "commit",
            "reason": "Retitle",
            **posted,
            "state_token": again.context["commit_form"]["state_token"].value(),
        },
    )
    assert final.status_code == 302
    catalog[0].refresh_from_db()
    assert catalog[0].title == "New title"


def test_a_file_that_changes_records_needs_a_reason_recorded_with_the_file(admin_user, catalog):
    data = csv("method_code,method_version,title", "EPA 200.8,5.4,Metals (2)")
    with pytest.raises(ValidationError) as refused:
        commit("methods", admin_user, data, "m.csv", reason="  ")
    assert "reason" in refused.value.message_dict
    commit("methods", admin_user, data, "m.csv", reason="Title as published")
    event = AuditEvent.objects.get(action="catalog.method.updated")
    digest = hashlib.sha256(data).hexdigest()
    assert event.reason == f"Title as published (import of m.csv, SHA-256 {digest})"
    assert event.before["title"] == "Metals by ICP-MS" and event.after["title"] == "Metals (2)"
    assert AuditEvent.objects.get(action=importing.SUMMARY_ACTION).reason == "Title as published"


# ---------------------------------------------------------------------- values and semantics


def test_exact_decimals_are_kept_to_the_stored_scale(admin_user, catalog):
    data = csv(
        "method_code,method_version,analyte,matrix,unit,reporting_limit",
        "EPA 200.8,5.4,Lead,wastewater,mg/L,0.00012345",
    )
    commit("tests", admin_user, data)
    test = TestDefinition.objects.get(matrix="wastewater")
    assert test.reporting_limit == Decimal("0.00012345")
    assert str(test.reporting_limit) == "0.00012345"
    limits = csv(
        "method_code,method_version,analyte,matrix,phase,qc_type,measure,lower,upper",
        "EPA 200.8,5.4,Lead,water,analysis,lcs,recovery,85.0000000001,115",
    )
    commit("qc-limits", admin_user, limits)
    limit = QCLimit.objects.get()
    assert limit.lower == Decimal("85.0000000001") and limit.upper == Decimal("115")
    too_fine = preview(
        "tests",
        admin_user,
        csv(
            "method_code,method_version,analyte,matrix,unit,reporting_limit",
            "EPA 200.8,5.4,Lead,soil,mg/kg,0.000000001",
        ),
    )
    assert too_fine.rows[0].errors == [
        "reporting_limit: Ensure that there are no more than 8 decimal places."
    ]


@pytest.mark.parametrize(
    "cell,message",
    [
        ("1e-3", "reporting_limit: '1e-3' is not a plain decimal number"),
        ("1,000", "Expected 6 cells, found 7."),
        ("0.5 mg/L", "reporting_limit: '0.5 mg/L' is not a plain decimal number"),
    ],
)
def test_malformed_numbers_are_errors_naming_line_and_column(admin_user, catalog, cell, message):
    data = csv(
        "method_code,method_version,analyte,matrix,unit,reporting_limit",
        f"EPA 200.8,5.4,Lead,soil,mg/kg,{cell}",
    )
    shown = preview("tests", admin_user, data)
    assert not shown.ok and shown.rows[0].line == 2
    assert shown.rows[0].errors[0].startswith(message)


def test_header_choice_and_duration_errors(admin_user, catalog):
    unknown = preview("analytes", admin_user, csv("name,colour", "Lead,grey"))
    assert unknown.file_errors[0].startswith("Line 1: unknown column(s) colour.")
    holding = csv(
        "method_code,method_version,matrix,start_event,end_event,name,duration_hours",
        "EPA 200.8,5.4,Water,collection,analysis,Metals,4320",
        "EPA 200.8,5.4,water,collection,analysis,Metals,6 months",
        "EPA 200.8,5.4,soil,collection,analysis,Metals,0.125",
    )
    rows = preview("holding-rules", admin_user, holding).rows
    assert rows[0].errors[0].startswith("matrix: 'Water' is not allowed. Use one of: water,")
    assert rows[1].errors == [
        "duration_hours: '6 months' is not a number of hours, for example 48 or 0.25."
    ]
    assert (
        rows[2].errors[0].startswith("duration_hours: Ensure that there are no more than 2 decimal")
    )
    good = csv(
        "method_code,method_version,matrix,start_event,end_event,name,duration_hours,warning_hours",
        "EPA 200.8,5.4,water,collection,analysis,Metals,4320,0.25",
    )
    commit("holding-rules", admin_user, good)
    rule = HoldingTimeRule.objects.get()
    assert rule.duration == timedelta(days=180) and rule.warning_window == timedelta(minutes=15)


def test_absent_column_is_left_alone_and_blank_clears_optional(admin_user, catalog):
    method = catalog[0]
    method.source = "SOP-12 rev 3"
    method.technique = "icp_ms"
    method.save()
    keep = csv("method_code,method_version,title", "EPA 200.8,5.4,Metals by ICP-MS")
    assert preview("methods", admin_user, keep).rows[0].action == "unchanged"
    clear = csv("method_code,method_version,source,technique", "epa 200.8,5.4,,")
    row = preview("methods", admin_user, clear).rows[0]
    assert row.changes == [("source", "SOP-12 rev 3", ""), ("technique", "icp_ms", "")]
    commit("methods", admin_user, clear, reason="Clear descriptors")
    method.refresh_from_db()
    assert (method.title, method.source, method.technique) == ("Metals by ICP-MS", "", "")
    required = preview(
        "methods", admin_user, csv("method_code,method_version,title", "EPA 200.8,5.4,")
    )
    assert required.rows[0].errors == ["title: cannot be blank."]
    missing = preview("methods", admin_user, csv("method_code,method_version", "EPA 300.0,2.1"))
    assert missing.rows[0].errors == [
        "title: required for a new method version; the column is missing from the file."
    ]


def test_identity_locks_are_the_services(admin_user, catalog):
    _, _, test = catalog
    QCLimit.objects.create(definition=test, qc_type="lcs", measure="recovery", lower=80, upper=120)
    data = csv("method_code,method_version,analyte,matrix,unit", "EPA 200.8,5.4,Lead,water,µg/L")
    row = preview("tests", admin_user, data).rows[0]
    assert row.action == "update" and row.changes == [("unit", "mg/L", "µg/L")]
    assert row.errors == [
        "unit: Locked: used by qc limits. Create a new version or deactivate this."
    ]
    test.refresh_from_db()
    assert test.unit == "mg/L"


def test_unknown_references_and_duplicates_are_row_errors(admin_user, catalog):
    data = csv(
        "method_code,method_version,analyte,matrix,unit",
        "EPA 200.8,5.4,Copper,water,mg/L",
        "EPA 999,1,Lead,water,mg/L",
        "EPA 200.8,5.4,lead,wastewater,mg/L",
        "epa 200.8,5.4,LEAD,wastewater,mg/L",
    )
    rows = preview("tests", admin_user, data).rows
    assert rows[0].errors == ["analyte: no analyte named 'Copper'. Import or create it first."]
    assert rows[1].errors == [
        "method_code, method_version: no method EPA 999 version 1. "
        "Import or create the method first."
    ]
    assert rows[2].ok and rows[2].action == "create"
    assert rows[3].errors == ["Duplicate of line 4: one row per record."]
    assert not TestDefinition.objects.filter(matrix="wastewater").exists()


# ------------------------------------------------------------------------ clients and projects


def test_clients_and_projects_through_receipt_services(manager, make_user):
    make_user("pm-jones", "project_manager")
    clients = csv(
        "account_code,name,email,state,po_required",
        "EXU,Example Utilities,lab@example.invalid,NY,yes",
    )
    commit("clients", manager, clients, "clients.csv")
    customer = Client.objects.get(account_number="EXU")
    assert customer.po_required and customer.state == "NY"
    assert AuditEvent.objects.get(action="client.created").reason.startswith(
        "Created by import of clients.csv"
    )
    projects = csv(
        "client_account_code,name,project_manager,turnaround_days",
        "EXU,Outfall monitoring,pm-jones,5",
    )
    commit("projects", manager, projects)
    project = Project.objects.get()
    assert project.client == customer and project.project_manager.username == "pm-jones"
    update = csv("client_account_code,name,turnaround_days", "EXU,outfall MONITORING,7")
    commit("projects", manager, update, reason="Agreed turnaround")
    project.refresh_from_db()
    assert project.turnaround_days == 7 and project.revision == 2
    assert project.name == "Outfall monitoring"  # the key identifies; it is never rewritten


def test_client_without_dependable_key_is_an_error_not_a_guess(manager):
    Client.objects.create(name="Example Utilities")
    shown = preview("clients", manager, csv("account_code,name", "EXU,example utilities"))
    assert (
        shown.rows[0]
        .errors[0]
        .startswith("name: a client named 'example utilities' already exists (no account code).")
    )
    customer = Client.objects.create(name="Example Water", account_number="EXW")
    Project.objects.create(client=customer, name="Plant")
    Project.objects.create(client=customer, name="plant")
    ambiguous = preview("projects", manager, csv("client_account_code,name", "EXW,Plant"))
    assert ambiguous.rows[0].errors == [
        "Project 'Plant' of client EXW matches more than one existing record; "
        "fix the records first."
    ]
    unknown = preview("projects", manager, csv("client_account_code,name", "NOPE,Plant"))
    assert unknown.rows[0].errors[0].startswith("client_account_code: no client with account code")
