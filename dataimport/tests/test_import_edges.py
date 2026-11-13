"""Edge cases from review: races, bounds, digits, encodings, tampered commits and stale records."""

import base64

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from analytical.models import QCRequirement
from catalog.models import Analyte, Method, TestDefinition
from core.models import AuditEvent
from dataimport import importing
from receipt import services as receipt_services
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
def method(db):
    return Method.objects.create(code="EPA 200.8", version="5.4", title="Metals")


# ------------------------------------------------------------------------------- findings


def test_a_project_created_after_the_file_was_read_is_not_created_twice(manager):
    customer = Client.objects.create(name="Example Utilities", account_number="EXU")
    new_fields = {"client": customer, "name": "Plant"}
    write = T["projects"].write
    write(T["projects"], manager, None, new_fields, {}, "First", None)
    with pytest.raises(ValidationError) as refused:  # the second import read "no project" too
        write(T["projects"], manager, None, new_fields | {"name": "PLANT"}, {}, "Second", None)
    assert "now has a project named 'PLANT'" in refused.value.message_dict["name"][0]
    assert Project.objects.filter(client=customer).count() == 1


def test_no_new_test_on_an_inactive_method_version(admin_user, method):
    Analyte.objects.create(name="Lead")
    method.is_active = False
    method.save()
    data = csv("method_code,method_version,analyte,matrix,unit", "EPA 200.8,5.4,Lead,water,mg/L")
    row = preview("tests", admin_user, data).rows[0]
    assert row.errors == ["method_code: This method version is inactive. Choose an active version."]
    assert not TestDefinition.objects.exists()


def test_numbers_beyond_the_field_bounds_are_row_errors_not_server_errors(
    client, admin_user, method, plain_static
):
    Analyte.objects.create(name="Lead")
    data = csv(
        "method_code,method_version,analyte,matrix,unit,reporting_limit",
        "EPA 200.8,5.4,Lead,water,mg/L,123456789012345678901",
    )
    row = preview("tests", admin_user, data).rows[0]
    assert row.errors == ["reporting_limit: Ensure that there are no more than 20 digits in total."]
    client.force_login(admin_user)
    page = client.post(
        reverse("dataimport:upload", args=["tests"]),
        {"step": "preview", "file": SimpleUploadedFile("t.csv", data)},
    )
    assert page.status_code == 200 and not page.context["preview"].ok


def test_an_unexpected_service_failure_is_a_row_error_and_writes_nothing(
    admin_user, method, monkeypatch
):
    def broken(**kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(importing.analytical_services, "save_config", broken)
    data = csv("method_code,method_version,qc_type,min_per_batch", "EPA 200.8,5.4,lcs,1")
    shown = preview("qc-requirements", admin_user, data)
    assert shown.rows[0].errors == [
        "Line 2 could not be saved because of an unexpected error. Nothing was imported; "
        "check the row's values or report the problem."
    ]
    with pytest.raises(ValidationError, match="Nothing was imported"):
        commit("qc-requirements", admin_user, data, token=shown.state_token)
    assert not QCRequirement.objects.exists() and not AuditEvent.objects.exists()

    def refused(**kwargs):
        raise PermissionDenied("synthetic")

    monkeypatch.setattr(importing.analytical_services, "save_config", refused)
    with pytest.raises(PermissionDenied):
        preview("qc-requirements", admin_user, data)


def test_two_new_clients_with_one_name_in_one_file_are_refused(manager):
    data = csv("account_code,name", "A1,Example Water", "A2,example water")
    rows = preview("clients", manager, data).rows
    assert rows[0].ok
    assert rows[1].errors[0].startswith("name: a client named 'example water' already exists (A1)")
    with pytest.raises(ValidationError):
        commit("clients", manager, data)
    assert not Client.objects.exists()


@pytest.mark.parametrize(
    "kind,header,cell",
    [
        ("tests", "method_code,method_version,analyte,matrix,unit,reporting_limit", "١٢.٥"),
        ("qc-requirements", "method_code,method_version,qc_type,min_per_batch", "２"),
    ],
)
def test_only_ascii_digits_are_numbers(admin_user, method, kind, header, cell):
    Analyte.objects.create(name="Lead")
    keys = "EPA 200.8,5.4,Lead,water,mg/L" if kind == "tests" else "EPA 200.8,5.4,lcs"
    row = preview(kind, admin_user, csv(header, f"{keys},{cell}")).rows[0]
    assert len(row.errors) == 1 and f"'{cell}' is not a" in row.errors[0]


# ---------------------------------------------------------------------------- review gaps


def test_previewing_updates_leaves_records_and_audit_untouched(admin_user, method):
    data = csv("method_code,method_version,title,active", "EPA 200.8,5.4,Renamed,no")
    shown = preview("methods", admin_user, data)
    assert shown.ok and shown.rows[0].action == "update"
    method.refresh_from_db()
    assert (method.title, method.is_active) == ("Metals", True)
    assert not AuditEvent.objects.exists()


def _previewed(client, kind, data):
    page = client.post(
        reverse("dataimport:upload", args=[kind]),
        {"step": "preview", "file": SimpleUploadedFile("f.csv", data)},
    )
    form = page.context["commit_form"]
    return {name: form[name].value() for name in ("filename", "payload", "state_token")}


def test_tampered_commits_are_revalidated(client, admin_user, method, plain_static):
    client.force_login(admin_user)
    data = csv("method_code,method_version,title", "EPA 200.8,5.4,Renamed")
    posted = _previewed(client, "methods", data)
    # The record type comes from the URL: the methods file is read as an analytes file.
    other = client.post(
        reverse("dataimport:upload", args=["analytes"]),
        {"step": "commit", "reason": "Why", **posted},
    )
    assert other.status_code == 200 and not other.context["preview"].ok
    assert b"Nothing was imported" in other.content
    url = reverse("dataimport:upload", args=["methods"])
    token = client.post(url, {"step": "commit", "reason": "Why", **posted, "state_token": "0" * 64})
    assert b"different from the preview" in token.content
    swapped = csv("method_code,method_version,title", "EPA 200.8,5.4,Something else")
    payload = base64.b64encode(swapped).decode()
    altered = client.post(url, {"step": "commit", "reason": "Why", **posted, "payload": payload})
    assert b"different from the preview" in altered.content
    assert altered.context["preview"].rows[0].changes == [("title", "Metals", "Something else")]
    garbage = client.post(url, {"step": "commit", **posted, "payload": "not base64!"})
    assert garbage.status_code == 200 and b"Nothing was imported" in garbage.content
    method.refresh_from_db()
    assert method.title == "Metals" and not AuditEvent.objects.exists()


def test_a_record_changed_between_preview_and_commit_is_refused_with_a_fresh_preview(
    client, manager, plain_static
):
    customer = receipt_services.create_client(
        actor=manager, name="Example Utilities", account_number="EXU"
    )
    client.force_login(manager)
    url = reverse("dataimport:upload", args=["clients"])
    posted = _previewed(client, "clients", csv("account_code,city", "EXU,Albany"))
    receipt_services.update_client(
        actor=manager, client_id=customer.pk, expected_revision=1, reason="Phone", phone="555"
    )
    page = client.post(url, {"step": "commit", "reason": "City", **posted})
    assert b"different from the preview" in page.content
    assert page.context["commit_form"]["state_token"].value() != posted["state_token"]
    customer.refresh_from_db()
    assert customer.city == "" and customer.revision == 2
    # Inside the commit the service still compares the revision read with the file.
    with pytest.raises(ValidationError, match="Client changed"):
        stale = Client.objects.get(pk=customer.pk)
        stale.revision = 1
        T["clients"].write(T["clients"], manager, stale, {}, {"city": "X"}, "City", None)


def test_byte_order_mark_mixed_case_headers_and_multi_line_cells(admin_user):
    text = (
        "﻿Method_Code, METHOD_VERSION ,Title,source\r\n"
        'EPA 1,1,One,"Line one\r\nline two"\r\n'
        "EPA 2,1,Two,\r\n"
    )
    shown = preview("methods", admin_user, text.encode("utf-8"))
    assert shown.ok and [row.line for row in shown.rows] == [2, 4]
    commit("methods", admin_user, text.encode("utf-8"))
    assert Method.objects.get(code="EPA 1").source == "Line one\r\nline two"


def test_a_file_that_is_not_utf8_is_refused(admin_user):
    shown = preview("analytes", admin_user, "name\r\nÉthane\r\n".encode("latin-1"))
    assert shown.file_errors == ["The file is not UTF-8 text. Export it as CSV (UTF-8)."]
    assert not shown.rows
