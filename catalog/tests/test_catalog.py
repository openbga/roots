"""Catalog rules: authorization, audit, edit locks, versions and the setup-to-bench path."""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from accounts.models import User
from catalog import services
from catalog.models import Analyte, Method, TestDefinition
from core.models import AuditEvent

pytestmark = pytest.mark.django_db


def events(action):
    return AuditEvent.objects.filter(action=action)


# ----------------------------------------------------------------------- authorization


def test_only_the_admin_role_changes_the_catalog(analyst):
    with pytest.raises(PermissionDenied):
        services.save_method(actor=analyst, instance=Method(code="X", version="1", title="X"))
    with pytest.raises(PermissionDenied):
        services.save_analyte(actor=analyst, instance=Analyte(name="Nope"))
    assert not Method.objects.exists() and not Analyte.objects.exists()
    assert not AuditEvent.objects.exists()


def test_pages_enforce_the_same_boundaries(client, analyst, admin_user, make_user, catalog_rows):
    from receipt.models import Client

    assert client.get(reverse("catalog:test-list")).status_code == 302  # to login
    client.force_login(analyst)
    assert client.get(reverse("catalog:test-list")).status_code == 200
    for name, args in (
        ("test-create", []),
        ("method-create", []),
        ("analyte-create", []),
        ("test-edit", [catalog_rows["tests"]["lead"].pk]),
        ("method-new-version", [catalog_rows["method"].pk]),
    ):
        url = reverse(f"catalog:{name}", args=args)
        assert client.get(url).status_code == 403
        assert client.post(url, {}).status_code == 403
    portal = make_user("portal", client=Client.objects.create(name="Example Client"))
    client.force_login(portal)
    assert client.get(reverse("catalog:test-list")).status_code == 403
    assert Method.objects.count() == 1


# ---------------------------------------------------------------------------- create


def test_create_method_analyte_and_test_with_exact_limit_and_audit(admin_user):
    method, _ = services.save_method(
        actor=admin_user,
        instance=Method(code="SM 2540 D", version="2015", title="Total suspended solids"),
    )
    analyte, _ = services.save_analyte(
        actor=admin_user, instance=Analyte(name="Total suspended solids")
    )
    test, _ = services.save_test_definition(
        actor=admin_user,
        instance=TestDefinition(
            method=method,
            analyte=analyte,
            matrix="wastewater",
            unit="mg/L",
            reporting_limit=Decimal("2.50000000"),
        ),
    )
    test.refresh_from_db()
    assert test.reporting_limit == Decimal("2.50000000")
    assert events("catalog.testdefinition.created").get().after["reporting_limit"] == "2.50000000"
    assert {e.actor for e in AuditEvent.objects.all()} == {admin_user}
    assert AuditEvent.objects.count() == 3


def test_validation_rejects_duplicates_bad_limits_and_bad_cas(admin_user, catalog_rows):
    with pytest.raises(ValidationError) as exc:
        services.save_method(
            actor=admin_user, instance=Method(code="syn-icp", version="R1", title="Again")
        )
    assert "version" in exc.value.message_dict
    with pytest.raises(ValidationError) as exc:
        services.save_analyte(actor=admin_user, instance=Analyte(name="LEAD (synthetic)"))
    assert "name" in exc.value.message_dict
    with pytest.raises(ValidationError) as exc:
        services.save_analyte(
            actor=admin_user, instance=Analyte(name="Zinc", cas_number="7440-66-5")
        )
    assert "cas_number" in exc.value.message_dict
    for bad in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValidationError) as exc:
            services.save_test_definition(
                actor=admin_user,
                instance=TestDefinition(
                    method=catalog_rows["method"],
                    analyte=catalog_rows["lead"],
                    matrix="soil",
                    unit="mg/kg",
                    reporting_limit=bad,
                ),
            )
        assert "reporting_limit" in exc.value.message_dict
    with pytest.raises(ValidationError) as exc:
        services.save_test_definition(
            actor=admin_user,
            instance=TestDefinition(
                method=catalog_rows["method"],
                analyte=catalog_rows["lead"],
                matrix="water",
                unit="mg/L",
            ),
        )
    assert "analyte" in exc.value.message_dict


# ------------------------------------------------------------------------ edit locks


def test_edits_need_a_reason_and_unused_rows_stay_editable(admin_user, catalog_rows):
    method = Method.objects.get(pk=catalog_rows["method"].pk)
    method.title = "Synthetic ICP-MS"
    with pytest.raises(ValidationError) as exc:
        services.save_method(actor=admin_user, instance=method)
    assert "reason" in exc.value.message_dict
    services.save_method(actor=admin_user, instance=method, reason="Correct title")
    event = events("catalog.method.updated").get()
    assert event.before["title"] == "Synthetic ICP" and event.after["title"] == "Synthetic ICP-MS"
    assert event.reason == "Correct title"
    _, changed = services.save_method(actor=admin_user, instance=method, reason="No-op")
    assert not changed and events("catalog.method.updated").count() == 1


def test_used_method_version_cannot_be_rewritten_only_retired(admin_user, catalog_rows, receive):
    receive([catalog_rows["tests"]["lead"]])
    method = Method.objects.get(pk=catalog_rows["method"].pk)
    assert services.method_locks(method)
    for field, value in (
        ("title", "Rewritten"),
        ("version", "r2"),
        ("code", "OTHER"),
        ("source", "Changed"),
    ):
        edited = Method.objects.get(pk=method.pk)
        setattr(edited, field, value)
        with pytest.raises(ValidationError) as exc:
            services.save_method(actor=admin_user, instance=edited, reason="Attempt")
        assert field in exc.value.message_dict and "Locked" in str(exc.value)
    method.refresh_from_db()
    assert (method.code, method.version, method.title) == ("SYN-ICP", "r1", "Synthetic ICP")
    edited = Method.objects.get(pk=method.pk)
    edited.is_active = False
    services.save_method(actor=admin_user, instance=edited, reason="Superseded")
    assert not Method.objects.get(pk=method.pk).is_active


def test_used_test_keeps_unit_and_identity_but_limit_and_status_change_audited(
    admin_user, catalog_rows, receive
):
    lead = catalog_rows["tests"]["lead"]
    receive([lead])
    edited = TestDefinition.objects.get(pk=lead.pk)
    edited.unit = "µg/L"
    with pytest.raises(ValidationError) as exc:
        services.save_test_definition(actor=admin_user, instance=edited, reason="Unit")
    assert "unit" in exc.value.message_dict
    edited = TestDefinition.objects.get(pk=lead.pk)
    edited.reporting_limit = Decimal("0.00250000")
    services.save_test_definition(actor=admin_user, instance=edited, reason="Lab MDL study")
    lead.refresh_from_db()
    assert lead.reporting_limit == Decimal("0.00250000") and lead.unit == "mg/L"
    event = events("catalog.testdefinition.updated").get()
    assert event.before["reporting_limit"] == "0.00500000"
    assert event.after["reporting_limit"] == "0.00250000"


def test_analyte_used_by_work_keeps_its_name_unused_does_not(admin_user, catalog_rows, receive):
    receive([catalog_rows["tests"]["lead"]])
    used = Analyte.objects.get(pk=catalog_rows["lead"].pk)
    used.name = "Pb"
    with pytest.raises(ValidationError):
        services.save_analyte(actor=admin_user, instance=used, reason="Rename")
    free = Analyte.objects.get(pk=catalog_rows["copper"].pk)
    free.name = "Copper, total"
    services.save_analyte(actor=admin_user, instance=free, reason="Clarify")
    assert Analyte.objects.get(pk=free.pk).name == "Copper, total"


# ------------------------------------------------------------------------- versions


def test_new_version_copies_active_tests_and_leaves_the_old_one_intact(
    admin_user, catalog_rows, receive
):
    receive([catalog_rows["tests"]["lead"]])
    catalog_rows["tests"]["copper"].is_active = False
    catalog_rows["tests"]["copper"].save()
    old = catalog_rows["method"]
    new = services.create_method_version(
        actor=admin_user,
        previous=old,
        version="r2",
        title="Synthetic ICP r2",
        source="SOP 2",
        copy_tests=True,
        retire_previous=True,
        reason="Instrument change",
    )
    copied = new.testdefinition_set.get()
    assert (copied.analyte, copied.unit, copied.reporting_limit) == (
        catalog_rows["lead"],
        "mg/L",
        Decimal("0.00500000"),
    )
    assert copied.pk != catalog_rows["tests"]["lead"].pk
    old.refresh_from_db()
    assert (old.version, old.title, old.is_active) == ("r1", "Synthetic ICP", False)
    assert old.testdefinition_set.count() == 2
    assert events("catalog.method.created").get().after["derived_from"] == old.pk
    assert events("catalog.testdefinition.created").get().after["copied_from"]
    with pytest.raises(ValidationError):
        services.create_method_version(
            actor=admin_user,
            previous=old,
            version="R2",
            title="t",
            source="",
            copy_tests=False,
            retire_previous=False,
            reason="dup",
        )
    assert Method.objects.count() == 2


# ------------------------------------------------------------- setup to bench, over HTTP


def test_setup_then_catalog_then_a_test_receipt_and_bench_can_use(client, make_user):
    from accounts.setup import setup_lab
    from receipt.models import Client as LabClient

    setup_lab(
        lab_name="Example Lab",
        username="owner",
        email="o@example.invalid",
        password="a-long-unique-passphrase-93",
        roles=["admin", "custodian", "analyst"],
    )
    assert client.login(username="owner", password="a-long-unique-passphrase-93")
    assert b"No tests defined yet" in client.get(reverse("catalog:test-list")).content

    created = client.post(
        reverse("catalog:method-create"),
        {
            "code": "SM 4500-NH3 G",
            "version": "2021",
            "title": "Ammonia by flow injection",
            "source": "Lab SOP-12 rev 3",
            "is_active": "on",
        },
    )
    assert created.status_code == 302
    client.post(reverse("catalog:analyte-create"), {"name": "Ammonia as N", "cas_number": ""})
    method, analyte = Method.objects.get(), Analyte.objects.get()
    bad = client.post(
        reverse("catalog:test-create"),
        {
            "method": method.pk,
            "analyte": analyte.pk,
            "matrix": "water",
            "unit": "mg/L",
            "reporting_limit": "0.1234567891",
            "is_active": "on",
        },
    )
    assert bad.status_code == 200 and b"decimal places" in bad.content  # input preserved
    ok = client.post(
        reverse("catalog:test-create"),
        {
            "method": method.pk,
            "analyte": analyte.pk,
            "matrix": "water",
            "unit": "mg/L",
            "reporting_limit": "0.05000000",
            "is_active": "on",
        },
    )
    assert ok.status_code == 302
    listing = client.get(reverse("catalog:test-list")).content
    assert b"Ammonia as N" in listing and b"0.05" in listing
    test = TestDefinition.objects.get()
    assert test.reporting_limit == Decimal("0.05000000")

    # Receipt offers it, and the bench can batch it.
    page = client.get(reverse("receipt:sample-create"))
    assert page.status_code == 200 and "Ammonia as N" in page.content.decode()
    assert test in page.context["form"].fields["tests"].queryset
    batch = client.post(
        reverse("analytical:batch-create"),
        {"method": method.pk, "matrix": "water", "prepared_at": "", "notes": ""},
    )
    assert batch.status_code == 302
    assert User.objects.get(username="owner").is_superuser
    assert not LabClient.objects.exists()


def test_every_page_renders_and_shows_locks_history_and_filters(
    client, admin_user, catalog_rows, receive
):
    receive([catalog_rows["tests"]["lead"]])
    renamed = Analyte.objects.get(pk=catalog_rows["copper"].pk)
    renamed.name = "Copper, total"
    services.save_analyte(actor=admin_user, instance=renamed, reason="Clarify")
    client.force_login(admin_user)
    method = catalog_rows["method"]
    pages = [
        reverse("catalog:test-list") + "?q=lead&matrix=water&status=active&sort=-method",
        reverse("catalog:method-list") + "?q=icp&sort=tests",
        reverse("catalog:analyte-list") + "?q=7439",
        reverse("catalog:test-edit", args=[catalog_rows["tests"]["lead"].pk]),
        reverse("catalog:method-edit", args=[method.pk]),
        reverse("catalog:analyte-edit", args=[catalog_rows["copper"].pk]),
        reverse("catalog:method-new-version", args=[method.pk]),
        reverse("catalog:test-create"),
    ]
    for url in pages:
        assert client.get(url).status_code == 200, url
    locked = client.get(reverse("catalog:method-edit", args=[method.pk]))
    assert not {"code", "title"} & set(locked.context["form"].fields)  # shown as facts, not inputs
    history = client.get(reverse("catalog:analyte-edit", args=[catalog_rows["copper"].pk]))
    assert b"Catalog analyte updated" in history.content  # readable title, not the code
    empty = client.get(reverse("catalog:analyte-list") + "?q=zzz").content
    assert b"No analytes match" in empty


def test_limit_display_is_trimmed_and_an_unchanged_edit_is_a_no_op(admin_user, catalog_rows):
    from catalog.templatetags.catalog_extras import plain_decimal

    assert [plain_decimal(Decimal(v)) for v in ("0.01000000", "2.00000000", "100.00000000")] == [
        "0.01",
        "2",
        "100",
    ]
    assert plain_decimal(None) == "—"
    lead = TestDefinition.objects.get(pk=catalog_rows["tests"]["lead"].pk)
    lead.reporting_limit = Decimal("0.005")  # what the trimmed form value parses to
    _, changed = services.save_test_definition(actor=admin_user, instance=lead, reason="Same")
    assert not changed and not events("catalog.testdefinition.updated").exists()
    lead.reporting_limit = Decimal("0.123456789")
    with pytest.raises(ValidationError, match="8 decimal places"):
        services.save_test_definition(actor=admin_user, instance=lead, reason="Too precise")
    assert services._plural("batch") == "batches" and services._plural("result") == "results"


def test_locked_form_ignores_tampering_and_asks_a_reason_only_for_changes(
    client, admin_user, catalog_rows, receive
):
    receive([catalog_rows["tests"]["lead"]])
    client.force_login(admin_user)
    method = catalog_rows["method"]
    url = reverse("catalog:method-edit", args=[method.pk])
    edit_form = client.get(url).context["form"]
    token = edit_form["original_state"].value()
    assert "reason" in edit_form.fields
    assert "reason" not in client.get(reverse("catalog:method-create")).context["form"].fields
    # Unchanged resubmission needs no reason; a real change without one is refused inline.
    assert client.post(url, {"is_active": "on", "original_state": token}).status_code == 302
    refused = client.post(url, {"title": "Rewritten", "is_active": ""})
    assert refused.status_code == 200 and "reason" in refused.context["form"].errors
    ok = client.post(
        url,
        {"title": "Rewritten", "is_active": "", "reason": "Superseded", "original_state": token},
    )
    assert ok.status_code == 302
    method.refresh_from_db()
    assert (method.title, method.is_active) == ("Synthetic ICP", False)
    assert events("catalog.method.updated").get().reason == "Superseded"


def test_grid_filters_sort_page_and_authorize_before_querying(
    client, admin_user, analyst, make_user, catalog_rows, receive
):
    from receipt.models import Client

    receive([catalog_rows["tests"]["lead"]])
    catalog_rows["tests"]["copper"].reporting_limit = None
    catalog_rows["tests"]["copper"].is_active = False
    catalog_rows["tests"]["copper"].save()
    url = reverse("catalog:test-list")

    def names(**params):
        return [t.analyte.name for t in client.get(url, params).context["page_obj"]]

    assert client.get(url).status_code == 302
    client.force_login(make_user("portal", client=Client.objects.create(name="Example Client")))
    assert client.get(url).status_code == 403
    client.force_login(analyst)
    assert names(sort="-analyte") == ["Lead (synthetic)", "Copper (synthetic)"]
    assert names(status="available", used="yes") == ["Lead (synthetic)"]
    assert names(limit="none") == ["Copper (synthetic)"]
    assert names(sort="__class__") == ["Copper (synthetic)", "Lead (synthetic)"]  # falls back
    response = client.get(url, {"status": "available", "q": "lead", "per_page": 50})
    assert response.context["page_obj"].paginator.per_page == 50
    chips = {c["label"]: c for c in response.context["active_filters"]}
    assert set(chips) == {"Search", "Status"}
    assert (
        "status=" not in chips["Status"]["remove_url"] and "q=lead" in chips["Status"]["remove_url"]
    )
    for bad in ({"per_page": "7"}, {"matrix": "bogus"}):
        page = client.get(url, bad)
        assert page.context["page_obj"].paginator.count == 0 and page.context["filter_form"].errors
    method = client.get(reverse("catalog:method-list")).context["page_obj"][0]
    assert (method.active_tests, method.requested, method.batches) == (1, 1, 0)


def test_new_version_warns_about_active_predecessor_and_counts_copied_tests(
    client, admin_user, catalog_rows
):
    client.force_login(admin_user)
    page = client.get(reverse("catalog:method-new-version", args=[catalog_rows["method"].pk]))
    fields = page.context["form"].fields
    assert fields["copy_tests"].label == "Copy the 2 active tests"
    assert "both versions stay active" in fields["retire_previous"].help_text


@pytest.mark.parametrize("kind", ["method", "analyte", "test"])
def test_stale_catalog_edits_keep_input_and_do_not_overwrite(
    client, admin_user, catalog_rows, kind
):
    client.force_login(admin_user)
    obj = {
        "method": catalog_rows["method"],
        "analyte": catalog_rows["copper"],
        "test": catalog_rows["tests"]["lead"],
    }[kind]
    url = reverse(f"catalog:{kind}-edit", args=[obj.pk])
    form = client.get(url).context["form"]
    data = {f.name: f.value() if f.value() is not None else "" for f in form}
    field = {"method": "title", "analyte": "name", "test": "unit"}[kind]
    data[field] = "My pending edit"
    data["reason"] = "Original edit"
    type(obj).objects.filter(pk=obj.pk).update(**{field: "Newer saved edit"})
    count = AuditEvent.objects.count()
    response = client.post(url, data)
    assert response.status_code == 200
    assert "changed after you opened" in str(response.context["form"].errors)
    assert response.context["form"][field].value() == "My pending edit"
    obj.refresh_from_db()
    assert getattr(obj, field) == "Newer saved edit"
    assert AuditEvent.objects.count() == count
    # Missing and forged tokens cannot bypass the check.
    for token in ("", "0" * 64):
        data["original_state"] = token
        assert client.post(url, data).status_code == 200
    assert AuditEvent.objects.count() == count


def test_a_catalog_row_opens_the_edit_form_only_for_users_who_may_edit(
    client, analyst, admin_user, catalog_rows
):
    """No record page: the identifier links to the edit form for an admin, plain text otherwise."""
    test = catalog_rows["tests"]["lead"]
    urls = {
        reverse("catalog:test-list"): reverse("catalog:test-edit", args=[test.pk]),
        reverse("catalog:method-list"): reverse("catalog:method-edit", args=[test.method.pk]),
        reverse("catalog:analyte-list"): reverse("catalog:analyte-edit", args=[test.analyte.pk]),
    }
    client.force_login(admin_user)
    for list_url, edit_url in urls.items():
        page = client.get(list_url).content.decode()
        assert f'class="vr-row-link" href="{edit_url}"' in page
        assert ">Edit<" not in page  # no trailing Edit link standing in for the identifier
    client.force_login(analyst)
    for list_url, edit_url in urls.items():
        page = client.get(list_url).content.decode()
        assert edit_url not in page and "vr-row-link" not in page


# ------------------------------------------------------------------ classification descriptors


def test_descriptors_are_saved_and_audited_on_create_and_edit(admin_user):
    method, _ = services.save_method(
        actor=admin_user,
        instance=Method(code="EPA 200.8", version="5.4", title="Metals by ICP-MS", reference="epa"),
    )
    analyte, _ = services.save_analyte(
        actor=admin_user, instance=Analyte(name="Arsenic (synthetic)", group="metals")
    )
    assert events("catalog.method.created").get().after["reference"] == "epa"
    assert events("catalog.analyte.created").get().after["group"] == "metals"
    method.technique = "icp_ms"
    with pytest.raises(ValidationError) as exc:
        services.save_method(actor=admin_user, instance=method)
    assert "reason" in exc.value.message_dict  # a descriptor change still needs a reason
    services.save_method(actor=admin_user, instance=method, reason="Classify")
    analyte.group = "other"
    services.save_analyte(actor=admin_user, instance=analyte, reason="Reclassify")
    event = events("catalog.method.updated").get()
    assert (event.before["technique"], event.after["technique"]) == ("", "icp_ms")
    assert event.after["reference"] == "epa" and event.reason == "Classify"
    assert events("catalog.analyte.updated").get().before["group"] == "metals"
    with pytest.raises(ValidationError):
        services.save_method(
            actor=admin_user, instance=Method(code="X", version="1", title="X", technique="bogus")
        )


def test_descriptors_stay_editable_when_work_locks_the_identity(
    client, admin_user, catalog_rows, receive
):
    receive([catalog_rows["tests"]["lead"]])
    method = Method.objects.get(pk=catalog_rows["method"].pk)
    analyte = Analyte.objects.get(pk=catalog_rows["lead"].pk)
    assert services.method_locks(method) and services.analyte_locks(analyte)
    method.reference, method.technique = "sm", "icp_oes"
    services.save_method(actor=admin_user, instance=method, reason="Classify")
    analyte.group = "metals"
    services.save_analyte(actor=admin_user, instance=analyte, reason="Classify")
    assert Method.objects.get(pk=method.pk).technique == "icp_oes"
    assert Analyte.objects.get(pk=analyte.pk).group == "metals"
    client.force_login(admin_user)
    for name, obj, field in (("method", method, "technique"), ("analyte", analyte, "group")):
        form = client.get(reverse(f"catalog:{name}-edit", args=[obj.pk])).context["form"]
        assert field in form.fields and form.locks
        assert form.fields[field].choices[0] == ("", "Not recorded")
    form = client.get(reverse("catalog:method-edit", args=[method.pk])).context["form"]
    response = client.post(
        reverse("catalog:method-edit", args=[method.pk]),
        {
            "technique": "gc_ms",
            "reference": "sm",
            "is_active": "on",
            "reason": "Reclassified",
            "original_state": form["original_state"].value(),
        },
    )
    assert response.status_code == 302
    assert Method.objects.get(pk=method.pk).technique == "gc_ms"


def test_new_version_inherits_reference_and_technique(client, admin_user, catalog_rows):
    old = catalog_rows["method"]
    old.reference, old.technique = "epa", "icp_ms"
    old.save()
    client.force_login(admin_user)
    url = reverse("catalog:method-new-version", args=[old.pk])
    form = client.get(url).context["form"]
    assert (form["reference"].value(), form["technique"].value()) == ("epa", "icp_ms")
    client.post(
        url,
        {
            "version": "r2",
            "title": "Synthetic ICP r2",
            "reference": "epa",
            "technique": "icp_oes",
            "reason": "Instrument change",
        },
    )
    new = Method.objects.get(version="r2")
    assert (new.reference, new.technique) == ("epa", "icp_oes")  # edited in the form
    assert events("catalog.method.created").get(object_id=str(new.pk)).after["technique"] == (
        "icp_oes"
    )
    inherited = services.create_method_version(
        actor=admin_user,
        previous=old,
        version="r3",
        title="r3",
        source="",
        copy_tests=False,
        retire_previous=False,
        reason="Revision",
    )
    assert (inherited.reference, inherited.technique) == ("epa", "icp_ms")
    old.refresh_from_db()
    assert old.technique == "icp_ms"


def test_lists_filter_by_technique_reference_and_analyte_group(client, analyst, catalog_rows):
    method = catalog_rows["method"]
    method.reference, method.technique = "epa", "icp_ms"
    method.save()
    Method.objects.create(code="SYN-IC", version="r1", title="Synthetic IC", technique="ic")
    catalog_rows["lead"].group = "metals"
    catalog_rows["lead"].save()
    client.force_login(analyst)

    def rows(name, **params):
        return [
            str(o) for o in client.get(reverse(f"catalog:{name}-list"), params).context["page_obj"]
        ]

    assert rows("method", technique="icp_ms") == ["SYN-ICP (r1)"]
    assert rows("method", technique="ic") == ["SYN-IC (r1)"]
    assert rows("method", reference="epa") == ["SYN-ICP (r1)"]
    assert len(rows("method")) == 2
    assert rows("analyte", group="metals") == ["Lead (synthetic)"]
    assert len(rows("analyte")) == 2
    assert rows("test", group="metals") == ["Lead (synthetic) · SYN-ICP (r1) · Water"]
    assert rows("test", group="voc") == []
    page = client.get(reverse("catalog:method-list"), {"technique": "icp_ms"})
    assert [c["label"] for c in page.context["active_filters"]] == ["Technique"]
    assert b"Metals" in client.get(reverse("catalog:analyte-list")).content
    bad = client.get(reverse("catalog:method-list"), {"technique": "bogus"})
    assert bad.context["page_obj"].paginator.count == 0 and bad.context["filter_form"].errors


def test_new_tests_need_an_active_method_version_but_existing_ones_stay_editable(admin_user):
    retired = Method.objects.create(code="X", version="1", title="Retired", is_active=False)
    active = Method.objects.create(code="X", version="2", title="Current")
    analyte = Analyte.objects.create(name="Inactive-method analyte")
    with pytest.raises(ValidationError) as refused:
        services.save_test_definition(
            actor=admin_user,
            instance=TestDefinition(method=retired, analyte=analyte, matrix="water", unit="mg/L"),
        )
    assert "inactive" in refused.value.message_dict["method"][0]
    existing = TestDefinition.objects.create(
        method=retired, analyte=analyte, matrix="soil", unit="mg/kg"
    )
    existing.is_active = False  # editing a test on a retired version stays allowed
    services.save_test_definition(actor=admin_user, instance=existing, reason="Retire")
    existing.method = active  # moving to an active version is fine, back to a retired one is not
    services.save_test_definition(actor=admin_user, instance=existing, reason="Move")
    existing.method = retired
    with pytest.raises(ValidationError):
        services.save_test_definition(actor=admin_user, instance=existing, reason="Back")
