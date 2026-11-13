"""Route coverage is a release gate; help stays inside each authorized page response."""

from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import URLResolver, get_resolver, resolve

from core.page_docs import (
    document_for_route,
    documentation_for_request,
    read_documents,
)

# Each exclusion is deliberately named: a newly mounted page must acquire help.
# Actions/downloads are explained on their invoking page, not a second endpoint.
NON_PAGE_ROUTES = {
    "health": "JSON process health probe",
    "ready": "JSON database readiness probe",
    "lookup": "JSON option lookup for remote selects (core/lookups.py)",
    "preference": "POST-only per-user presentation setting (core/preferences.py)",
    "mfa_download_recovery_codes": "Private text download from recovery-code help",
    "admin:login": "Redirect to the application sign-in page",
    "admin:autocomplete": "JSON related-object lookup",
    "admin:jsi18n": "JavaScript translation asset",
    "admin:view_on_site": "Redirect to the object's application URL",
    "admin:accounts_user_delete": "Deletion disabled by AccountAdmin",
    "admin:mfa_authenticator_add": "Creation disabled by AuthenticatorAdmin",
    "admin:account_emailaddress_add": "Creation disabled by EmailAddressAdmin",
    "admin:account_emailaddress_delete": "Deletion disabled by EmailAddressAdmin",
    "admin:auth_group_delete": "Deletion disabled by RoleGroupAdmin",
    "admin:core_lab_add": "Creation disabled by LabAdmin",
    "admin:core_lab_delete": "Deletion disabled by LabAdmin",
    "announcement-withdraw": "POST action on the announcement list",
    "lab-logo-remove": "POST action on the laboratory profile",
    "lab-logo-image": "Logo image previewed on the laboratory profile",
    "lab-certificate-remove": "POST action on the laboratory profile",
    "receipt:subcontract-source-download": "Private PDF on subcontract detail",
    "receipt:order-labels": "PDF labels from order detail",
    "receipt:sample-labels": "PDF labels from sample detail",
    "receipt:order-cancel": "POST action on order detail",
    "receipt:order-reinstate": "POST action on order detail",
    "commercial:quote-issue": "POST action on quote detail",
    "commercial:quote-revise": "POST action on quote detail",
    "commercial:quote-withdraw": "POST action on quote detail",
    "commercial:submission-labels": "PDF labels from submission detail",
    "commercial:submission-return": "POST action on submission detail",
    "commercial:submission-cancel": "POST action on submission detail",
    "commercial:submission-reinstate": "POST action on submission detail",
    "commercial:submission-accept": "POST action on submission detail",
    "client_commercial:quote-accept": "POST action on portal quote detail",
    "client_commercial:quote-decline": "POST action on portal quote detail",
    "client_commercial:submission-submit": "POST action on portal submission detail",
    "client_commercial:submission-discard": "POST action on portal submission detail",
    "client_commercial:sample-delete": "POST action on portal submission detail",
    "analytical:prep-close": "POST action on preparation detail",
    "analytical:batch-remove-test": "POST action on batch detail",
    "analytical:batch-close": "POST action on batch detail",
    "analytical:batch-preparation": "POST preparation link form on batch detail",
    "analytical:batch-equipment": "POST equipment link form on batch detail",
    "analytical:batch-submit": "POST action on batch detail",
    "analytical:mdl-close": "POST action on MDL detail",
    "laboratory:evidence": "Private evidence download on laboratory records",
    "reporting:report-preview-pdf": "Draft PDF within report preview",
    "reporting:report-release": "POST action on report preview",
    "reporting:version-pdf": "Stored PDF from report detail",
    "reporting:edd-download": "Stored ZIP from EDD preview",
    "portal:report-pdf": "Client-scoped stored PDF from report detail",
    "portal:edd-download": "Client-scoped stored EDD ZIP from report detail",
    "dataimport:template": "CSV template download from spreadsheet import pages",
    "dataimport:starter": "Starter CSV download from the spreadsheet import overview",
}


def named_routes(patterns, namespace=""):
    routes = set()
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            prefix = namespace + (pattern.namespace + ":" if pattern.namespace else "")
            routes |= named_routes(pattern.url_patterns, prefix)
        elif pattern.name:
            routes.add(namespace + pattern.name)
    return routes


def test_every_page_route_has_documentation_and_exclusions_are_current():
    routes = named_routes(get_resolver().url_patterns)
    covered = {route for doc in read_documents() for route in doc["routes"]}
    assert not routes - covered - NON_PAGE_ROUTES.keys(), "Add page help for these routes"
    assert not NON_PAGE_ROUTES.keys() - routes, "Remove stale non-page exclusions"
    assert not covered - routes - {"documentation:shared", "documentation:unavailable"}


def test_content_has_valid_sources_and_no_ambiguous_variant_matches():
    root = Path(__file__).resolve().parent.parent
    docs = read_documents()  # Also validates shape, nonempty entries and duplicate mappings.
    assert docs
    for doc in docs:
        for source in doc["sources"]:
            assert (root / source).is_file(), (doc["id"], source)
        # Two equally-specific predicates for one route must not overlap.
        for other in docs:
            if doc["id"] >= other["id"] or not set(doc["routes"]) & set(other["routes"]):
                continue
            left, right = doc.get("match", {}), other.get("match", {})
            if len(left) == len(right):
                assert any(k in right and right[k] != v for k, v in left.items()), (
                    doc["id"],
                    other["id"],
                )


def test_dynamic_forms_resolve_to_their_own_guide():
    from analytical.forms import CONFIG_KINDS
    from dataimport.importing import BY_SLUG
    from receipt.correction_views import SECTIONS

    for route, key, values in (
        ("receipt:sample-correct", "section", SECTIONS),
        ("analytical:config-new", "kind", CONFIG_KINDS),
        ("analytical:config-edit", "kind", CONFIG_KINDS),
        ("dataimport:upload", "kind", BY_SLUG),
    ):
        ids = []
        for value in values:
            doc = document_for_route(route, {key: value, "pk": 1})
            assert doc is not None, (route, value)
            assert doc["match"][key] == value
            ids.append(doc["id"])
        assert len(ids) == len(set(ids))
        assert document_for_route(route, {key: "not-a-real-kind"}) is None
    for action in ("advance", "return", "reopen"):
        doc = document_for_route("laboratory:nc-step", {"action": action, "pk": 1})
        assert doc is not None
        assert doc["match"]["action"] == action


def test_documentation_is_escaped_and_evidence_is_not_exposed():
    content = {
        "id": "safe",
        "title": "<script>bad()</script>",
        "purpose": "<img src=x>",
        "sources": ["private-evidence-path"],
        "sections": [
            {
                "title": "<b>Heading</b>",
                "paragraphs": ["<iframe>"],
                "items": [{"label": "<i>Field</i>", "description": "<svg onload=x>"}],
            }
        ],
    }
    html = render_to_string("documentation/content.html", {"page_documentation": content})
    # The drawer's own icons are <svg class="vr-icon">; the injected one must stay text.
    for unsafe in ("<script>", "<img", "<iframe>", "<svg onload", "private-evidence-path"):
        assert unsafe not in html
    assert "&lt;script&gt;" in html


def test_paragraph_only_sections_do_not_render_empty_dictionary_items_tables():
    paragraph_only = next(
        section
        for doc in read_documents()
        for section in doc["sections"]
        if section["paragraphs"] and not section["items"]
    )
    html = render_to_string(
        "documentation/content.html",
        {"page_documentation": {"title": "Notes", "sections": [paragraph_only]}},
    )
    assert "<table" not in html
    assert paragraph_only["title"] in html


def test_failed_release_keeps_preview_documentation():
    assert document_for_route("reporting:report-release") == document_for_route(
        "reporting:report-preview"
    )
    for route in ("client_commercial:quote-accept", "client_commercial:quote-decline"):
        assert document_for_route(route) == document_for_route("client_commercial:quote-detail")


@pytest.mark.django_db
def test_error_pages_replace_resolved_page_help_and_500_needs_no_database(
    django_assert_num_queries,
):
    from django.contrib.auth.models import AnonymousUser
    from django.views.defaults import page_not_found, permission_denied, server_error

    request = RequestFactory().get("/clients/123/")
    request.user = AnonymousUser()
    request.resolver_match = resolve(request.path)
    for view in (permission_denied, page_not_found):
        response = view(request, Exception("Synthetic error"))
        assert b'data-vr-documentation-id="core-unavailable"' in response.content
        assert b'data-vr-documentation-id="receipt-client-detail"' not in response.content
    with django_assert_num_queries(0):
        response = server_error(request)
    assert b"data-vr-documentation-toggle" in response.content
    assert b'data-vr-documentation-id="core-unavailable"' in response.content


def test_request_help_uses_route_not_query_or_record_data():
    request = RequestFactory().get("/clients/?documentation=admin:accounts_user_change")
    request.resolver_match = resolve("/clients/")
    data = documentation_for_request(request)
    assert data["id"] == document_for_route("receipt:client-list")["id"]
    assert set(data) == {"id", "title", "purpose", "sections"}
    request.resolver_match = None
    assert documentation_for_request(request)["id"] == "core-unavailable"


@pytest.mark.django_db
def test_help_follows_authorized_page_and_form_fragments(client, staff):
    user = staff("docs-staff", "project_manager")
    client.force_login(user)
    for url, route in (
        ("/clients/", "receipt:client-list"),
        ("/clients/new/", "receipt:client-create"),
    ):
        response = client.get(url)
        assert response.status_code == 200
        assert response.context["page_documentation"]["id"] == document_for_route(route)["id"]
    fragment = client.get("/clients/new/", HTTP_X_VR_FORM="1")
    assert b"data-vr-documentation-source" in fragment.content
    from accounts.models import User
    from receipt.models import Client

    customer = Client.objects.create(name="Synthetic docs client")
    portal_user = User.objects.create_user("docs-client", client=customer)
    client.force_login(portal_user)
    assert client.get("/clients/").status_code == 403
    portal = client.get("/portal/")
    assert portal.status_code == 200
    assert portal.context["page_documentation"]["id"] == document_for_route("portal:home")["id"]


@pytest.mark.django_db
def test_account_and_administration_screens_include_help(client, staff):
    response = client.get("/accounts/login/")
    assert response.context["page_documentation"]["id"] == "accounts-login"
    user = staff("docs-owner", "admin")
    user.is_staff = user.is_superuser = True
    user.save()
    client.force_login(user)
    response = client.get("/admin/accounts/user/")
    assert response.status_code == 200
    assert response.context["page_documentation"]["id"] == "admin-users"
    assert b"data-vr-documentation-toggle" in response.content
    assert b'id="vr-documentation"' in response.content


# ---------------------------------------------------------------- the friendly layer

FRIENDLY = {
    "summary": "Find a quote and see where it stands.",
    "tasks": [
        {"title": "Find a quote", "steps": ["Type a number in [[Search]].", "Select it."]},
        {"title": "Start a quote", "steps": ["Select [[New quote]]."]},
    ],
    "good_to_know": ["One.", "Two.", "Three."],
}


def friendly_document(**changes):
    document = {
        "id": "friendly",
        "title": "Quotes",
        "purpose": "Quotes.",
        "routes": ["friendly"],
        "sources": ["synthetic"],
        "sections": [{"title": "Fields", "items": [{"label": "Search", "description": "Finds."}]}],
        **FRIENDLY,
    }
    document.update(changes)
    return document


def check(document):
    from core.page_docs import check_friendly

    check_friendly("synthetic.json", document)


def test_friendly_layer_is_optional_but_all_or_none():
    check(friendly_document())
    plain = friendly_document()
    for key in FRIENDLY:
        del plain[key]
    check(plain)  # no layer at all is fine
    for key in FRIENDLY:
        partial = friendly_document()
        del partial[key]
        with pytest.raises(ImproperlyConfigured, match="come together"):
            check(partial)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"summary": "x" * 241}, "summary"),
        ({"summary": "Select [[Apply]]."}, "summary is plain text"),
        ({"tasks": FRIENDLY["tasks"][:1]}, "tasks needs 2-5"),
        ({"tasks": FRIENDLY["tasks"] * 3}, "tasks needs 2-5"),
        ({"tasks": [{"title": "x" * 81, "steps": ["a"]}, FRIENDLY["tasks"][1]]}, "title"),
        ({"tasks": [{"title": "Do", "steps": []}, FRIENDLY["tasks"][1]]}, "1-4 steps"),
        ({"tasks": [{"title": "Do", "steps": ["a"] * 5}, FRIENDLY["tasks"][1]]}, "1-4 steps"),
        ({"tasks": [{"title": "Do", "steps": ["x" * 201]}, FRIENDLY["tasks"][1]]}, "over 200"),
        ({"tasks": [{"title": "Do", "steps": [" "]}, FRIENDLY["tasks"][1]]}, "non-blank"),
        ({"good_to_know": ["One.", "Two."]}, "good_to_know needs 3-5"),
        ({"good_to_know": ["a"] * 6}, "good_to_know needs 3-5"),
        ({"good_to_know": ["One.", "Two.", "x" * 201]}, "over 200"),
        ({"good_to_know": ["Open [[Columns.", "Two.", "Three."]}, "closing"),
        ({"good_to_know": ["[[a [[b]] c]]", "Two.", "Three."]}, "closing"),
        ({"good_to_know": ["Stray ]] here.", "Two.", "Three."]}, "closing"),
    ],
)
def test_friendly_layer_limits_and_labels_are_enforced(changes, message):
    with pytest.raises(ImproperlyConfigured, match=message):
        check(friendly_document(**changes))


def test_the_drawer_receives_the_friendly_layer_but_not_evidence():
    from core.page_docs import documentation_for_route

    help_ = documentation_for_route("commercial:quote-list")
    assert help_["summary"] and help_["tasks"] and help_["good_to_know"]
    assert "sources" not in help_ and "routes" not in help_


def test_labels_render_as_chips_after_escaping():
    from core.templatetags.documentation import help_text

    assert help_text("Select [[New quote]].") == 'Select <span class="vr-help-ui">New quote</span>.'
    assert help_text("<b>[[x]]</b>") == '&lt;b&gt;<span class="vr-help-ui">x</span>&lt;/b&gt;'
    assert help_text("Use [[<i>]].") == 'Use <span class="vr-help-ui">&lt;i&gt;</span>.'
    assert help_text("no markers") == "no markers"


def test_friendly_page_renders_summary_tasks_tips_and_collapsed_reference():
    html = render_to_string(
        "documentation/content.html", {"page_documentation": friendly_document()}
    )
    assert "Find a quote and see where it stands." in html
    assert 'href="#vr-help-tasks"' in html and 'id="vr-help-know"' in html
    assert html.count('<span class="vr-help-ui">') == 2
    assert '<details class="vr-help-ref" data-vr-help-section>' in html  # closed
    assert 'id="vr-help-filter"' in html


def test_page_without_the_layer_shows_purpose_and_open_reference():
    document = friendly_document()
    for key in FRIENDLY:
        del document[key]
    html = render_to_string("documentation/content.html", {"page_documentation": document})
    assert '<p class="vr-help-summary">Quotes.</p>' in html
    assert "vr-help-jump" not in html and "vr-help-tasks" not in html
    assert '<details class="vr-help-ref" open data-vr-help-section>' in html
    assert "<dt>Search</dt>" in html
