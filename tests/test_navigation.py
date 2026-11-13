"""Navigation contract (docs/ui-standards.md section 3 and section 11).

Every named route is classified exactly once: a page that marks one sidebar or portal item (the
mapping in core/templatetags/navigation.py), an account page in the user's shell that marks none,
an entrance page without the shell, or an endpoint that is not a page. Routes that render without
URL arguments (lists, overviews, create forms) are rendered and must mark exactly their declared
item; routes that need a record are checked through the declaration, because the same tag sets
the mark on every page from the resolved view name.
"""

import html
import re

import pytest
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.core.exceptions import SuspiciousOperation
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import NoReverseMatch, get_resolver, reverse
from django.urls.resolvers import URLResolver
from django.views.defaults import bad_request

from accounts.models import User
from core.templatetags.navigation import (
    ACCOUNT_ROUTES,
    ITEMS,
    PORTAL_NAV,
    ROUTE_ITEMS,
    SECTION_TABS,
    SHARED_ROUTES,
    STAFF_MENU_ROLES,
    STAFF_NAV,
    STAFF_SECTIONS,
)
from receipt.models import Client

pytestmark = pytest.mark.django_db

# allauth pages in the centred entrance layout: no shell, no navigation.
ENTRANCE_ROUTES = {
    "account_login",
    "account_request_login_code",
    "account_confirm_login_code",
    "account_signup",
    "account_inactive",
    "account_reset_password",
    "account_reset_password_done",
    "account_reset_password_from_key",
    "account_reset_password_from_key_done",
    "account_confirm_email",
    "account_email_verification_sent",
    "account_reauthenticate",
    "mfa_authenticate",
    "mfa_reauthenticate",
}

# Not pages: POST-only actions that redirect, and downloads, PDFs, archives and probes.
NON_PAGE_ROUTES = {
    "announcement-withdraw",
    "lab-logo-remove",
    "lab-logo-image",
    "lab-certificate-remove",
    "receipt:order-cancel",
    "receipt:order-reinstate",
    "commercial:quote-issue",
    "commercial:quote-revise",
    "commercial:quote-withdraw",
    "commercial:submission-return",
    "commercial:submission-cancel",
    "commercial:submission-reinstate",
    "commercial:submission-accept",
    "analytical:prep-close",
    "analytical:batch-remove-test",
    "analytical:batch-close",
    "analytical:batch-preparation",
    "analytical:batch-equipment",
    "analytical:batch-submit",
    "analytical:mdl-close",
    "reporting:report-release",
    "client_commercial:quote-accept",
    "client_commercial:quote-decline",
    "client_commercial:submission-submit",
    "client_commercial:submission-discard",
    "client_commercial:sample-delete",
    "commercial:submission-labels",
    "health",
    "lookup",
    "preference",
    "ready",
    "laboratory:evidence",
    "portal:edd-download",
    "portal:report-pdf",
    "receipt:order-labels",
    "receipt:sample-labels",
    "receipt:subcontract-source-download",
    "reporting:edd-download",
    "reporting:report-preview-pdf",
    "reporting:version-pdf",
    "mfa_download_recovery_codes",
}

# Items whose label does not yet equal the title (h1) of the page they open. The page title is
# what changes (phase D); delete the entry with that change. Stale entries fail the test.
TITLE_MISMATCHES = {}

STAFF_KEYS = {item.key for item in STAFF_NAV}
PORTAL_KEYS = {item.key for item in PORTAL_NAV}


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Render real templates without a collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def admin_user(staff):
    return staff("nav-admin", "admin")


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        "nav-client",
        "nav-client@example.invalid",
        "test-password-729!",
        client=Client.objects.create(name="Example Client"),
    )


def route_names(resolver=None, namespace=None):
    """Every named route outside Django admin, as view names (namespace:name)."""
    resolver = resolver or get_resolver()
    for pattern in resolver.url_patterns:
        if isinstance(pattern, URLResolver):
            ns = namespace
            if pattern.namespace:
                ns = f"{namespace}:{pattern.namespace}" if namespace else pattern.namespace
            if ns == "admin":
                continue
            yield from route_names(pattern, ns)
        elif pattern.name:
            yield f"{namespace}:{pattern.name}" if namespace else pattern.name


def no_argument_url(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return None


def text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def region(content, start):
    page = content.decode()
    begin = page.find(start)
    return page[begin : page.index("</nav>", begin)] if begin >= 0 else None


def sidebar_current(response):
    nav = region(response.content, '<nav class="vr-sidenav')
    assert nav is not None, "staff shell expected"
    return [
        text(label)
        for label in re.findall(
            r'<a class="vr-navlink"[^>]*aria-current="page"[^>]*>(.*?)</a>', nav, re.S
        )
    ]


def portal_current(response):
    nav = region(response.content, '<nav class="vr-portal-nav"')
    assert nav is not None, "portal shell expected"
    return [
        text(label)
        for label in re.findall(r'<a class="nav-link"[^>]*aria-current="page"[^>]*>(.*?)</a>', nav)
    ]


def heading(response):
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", response.content.decode(), re.S)
    return text(match.group(1)) if match else None


def test_every_route_is_classified_exactly_once():
    names = set(route_names())
    classes = {
        "item": set(ROUTE_ITEMS),
        "account": set(ACCOUNT_ROUTES),
        "entrance": ENTRANCE_ROUTES,
        "not a page": NON_PAGE_ROUTES,
    }
    problems = []
    for name in sorted(names):
        found = [label for label, members in classes.items() if name in members]
        if len(found) != 1:
            problems.append(f"{name}: {found or 'unclassified'}")
    for label, members in classes.items():
        problems += [f"{name}: declared as {label} but not a route" for name in members - names]
    # Django admin renders its own chrome; its item only links there. allauth registers the
    # request-a-code page only when emailed sign-in codes are enabled.
    optional = {
        "admin:index: declared as item but not a route",
        "account_request_login_code: declared as entrance but not a route",
    }
    problems = [p for p in problems if p not in optional]
    assert not problems, "\n".join(problems)


def test_shared_routes_name_declared_routes_and_other_items():
    for route, alternatives in SHARED_ROUTES.items():
        assert route in ROUTE_ITEMS, route
        assert alternatives and alternatives <= STAFF_KEYS, route
        assert ROUTE_ITEMS[route] not in alternatives, route


def test_every_staff_destination_has_an_explicit_menu_rule_and_one_section():
    grouped = [key for _, _, keys in STAFF_SECTIONS for key in keys]
    assert len(grouped) == len(set(grouped))
    assert set(grouped) == set(STAFF_MENU_ROLES) == STAFF_KEYS


COMMON_MENU = {"dashboard", "training", "nonconformances"}
ROLE_MENUS = {
    "custodian": COMMON_MENU
    | {"client-submissions", "orders", "samples", "storage", "schedule", "subcontracts"},
    "analyst": COMMON_MENU
    | {
        "worklist",
        "prep",
        "batches",
        "subcontracts",
        "mdl",
        "charts",
        "lab-overview",
        "inventory",
        "equipment",
        "import-mappings",
    },
    "reviewer": COMMON_MENU
    | {
        "samples",
        "prep",
        "batches",
        "subcontracts",
        "mdl",
        "charts",
        "review",
        "lab-overview",
        "inventory",
        "equipment",
    },
    "approver": COMMON_MENU | {"reports"},
    "project_manager": COMMON_MENU
    | {
        "quotes",
        "price-lists",
        "client-submissions",
        "clients",
        "projects",
        "orders",
        "samples",
        "storage",
        "schedule",
        "subcontracts",
        "reports",
    },
    "admin": STAFF_KEYS - {"accounts-roles"},
}


def sidebar_links(response):
    nav = region(response.content, '<nav class="vr-sidenav')
    assert nav is not None
    links = re.findall(r'<a class="vr-navlink" href="([^"]+)"', nav)
    assert len(links) == len(set(links)), "Menu destinations must not repeat"
    return {html.unescape(link) for link in links}


def menu_urls(keys):
    return {reverse(ITEMS[key].url_name) for key in keys}


@pytest.mark.parametrize("role", ROLE_MENUS)
def test_staff_menu_matches_work_capabilities_and_every_destination_opens(client, staff, role):
    client.force_login(staff(f"menu-{role}", role))
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    assert sidebar_links(response) == menu_urls(ROLE_MENUS[role])
    nav = region(response.content, '<nav class="vr-sidenav')
    # Hidden destinations must not leave empty headings or inaccessible list labels behind.
    for key, label, keys in STAFF_SECTIONS:
        if label:
            assert (f'id="vr-nav-{key}"' in nav) == bool(set(keys) & ROLE_MENUS[role])
    for url in sidebar_links(response):
        assert client.get(url).status_code == 200, (role, url)


def test_combined_roles_and_revocations_change_the_next_menu(client, staff):
    user = staff("combined-menu", "analyst")
    user.groups.add(Group.objects.get_or_create(name="custodian")[0])
    client.force_login(user)
    assert sidebar_links(client.get(reverse("dashboard"))) == menu_urls(
        ROLE_MENUS["analyst"] | ROLE_MENUS["custodian"]
    )
    user.groups.remove(Group.objects.get(name="custodian"))
    assert sidebar_links(client.get(reverse("dashboard"))) == menu_urls(ROLE_MENUS["analyst"])


@pytest.mark.parametrize("group_grant", [False, True])
def test_explicit_release_grant_adds_reports_and_revocation_removes_it(client, staff, group_grant):
    reviewer = staff("release-menu", "reviewer")
    permission = Permission.objects.get(
        content_type__app_label="accounts", codename="authorize_release"
    )
    grants = reviewer.groups.get().permissions if group_grant else reviewer.user_permissions
    client.force_login(reviewer)
    assert sidebar_links(client.get(reverse("dashboard"))) == menu_urls(ROLE_MENUS["reviewer"])
    grants.add(permission)
    assert sidebar_links(client.get(reverse("dashboard"))) == menu_urls(
        ROLE_MENUS["reviewer"] | {"reports"}
    )
    grants.remove(permission)
    assert sidebar_links(client.get(reverse("dashboard"))) == menu_urls(ROLE_MENUS["reviewer"])


def test_hidden_readable_page_does_not_restore_its_menu_or_grant_actions(client, staff):
    client.force_login(staff("analyst-related", "analyst"))
    response = client.get(reverse("receipt:sample-list"))
    assert response.status_code == 200  # related records retain their existing read access
    assert sidebar_links(response) == menu_urls(ROLE_MENUS["analyst"])
    assert sidebar_current(response) == []
    assert client.get(reverse("receipt:sample-create")).status_code == 403
    assert client.post(reverse("receipt:sample-create"), {}).status_code == 403


@pytest.mark.parametrize(
    "kind", ["anonymous", "inactive", "client", "roleless", "owner", "superuser"]
)
def test_staff_menu_respects_account_boundaries(staff, kind):
    user = AnonymousUser() if kind == "anonymous" else User.objects.create_user(f"boundary-{kind}")
    if kind == "inactive":
        user.groups.add(Group.objects.get_or_create(name="admin")[0])
        user.is_active = False
    elif kind == "client":
        user.client = Client.objects.create(name="Example menu client")
        user.groups.add(Group.objects.get_or_create(name="admin")[0])
        user.is_staff = user.is_superuser = True
    elif kind in {"owner", "superuser"}:
        user.is_superuser = True
        user.is_staff = kind == "owner"
    request = RequestFactory().get("/")
    request.user = user
    rendered = render_to_string("includes/nav_staff.html", {"request": request})
    links = re.findall(r'<a class="vr-navlink" href="([^"]+)"', rendered)
    assert links == ([reverse("admin:index")] if kind == "owner" else [])


def test_pages_without_arguments_mark_their_declared_item(client, admin_user, client_user):
    rendered = 0
    for name, key in ROUTE_ITEMS.items():
        url = no_argument_url(name) if not name.startswith("admin:") else None
        if url is None:
            continue  # needs a record: covered by the declaration test above
        portal = key in PORTAL_KEYS
        client.force_login(client_user if portal else admin_user)
        response = client.get(url)
        assert response.status_code == 200, (name, response.status_code)
        current = portal_current(response) if portal else sidebar_current(response)
        assert current == [ITEMS[key].label], (name, current)
        rendered += 1
    assert rendered >= 50  # every list, overview and create form; fewer means routes went missing


def test_item_labels_equal_their_page_titles(client, admin_user, client_user):
    problems = []
    for item in STAFF_NAV + PORTAL_NAV:
        if item.url_name.startswith("admin:"):
            continue
        client.force_login(client_user if item.key in PORTAL_KEYS else admin_user)
        title = heading(client.get(reverse(item.url_name)))
        expected = TITLE_MISMATCHES.get(item.key, item.label)
        if title != expected:
            problems.append(f"{item.key}: label {item.label!r}, page title {title!r}")
        if item.key in TITLE_MISMATCHES and title == item.label:
            problems.append(f"{item.key}: matches now; delete it from TITLE_MISMATCHES")
    assert not problems, "\n".join(problems)


def test_account_pages_keep_the_shell_and_mark_nothing(client, admin_user, client_user):
    url = reverse("account_change_password")
    client.force_login(admin_user)
    assert sidebar_current(client.get(url)) == []
    client.force_login(client_user)
    assert portal_current(client.get(url)) == []


def test_error_pages_keep_the_shell_for_signed_in_users(client, admin_user, client_user):
    missing = "/no-such-page/"
    anonymous = client.get(missing)
    assert anonymous.status_code == 404
    assert b"vr-entrance" in anonymous.content and b"vr-sidenav" not in anonymous.content
    assert heading(anonymous) == "Page not found"

    client.force_login(admin_user)
    signed_in = client.get(missing)
    assert signed_in.status_code == 404
    assert sidebar_current(signed_in) == [] and heading(signed_in) == "Page not found"

    client.force_login(client_user)
    denied = client.get(reverse("receipt:sample-list"))
    assert denied.status_code == 403
    assert portal_current(denied) == []
    assert heading(denied) == "You do not have access to this page"

    request = RequestFactory().get("/")
    request.user = admin_user
    request.session = {}
    rejected = bad_request(request, SuspiciousOperation())
    assert rejected.status_code == 400
    assert sidebar_current(rejected) == []
    assert heading(rejected) == "The request could not be accepted"


def test_section_tabs_stay_under_one_item_and_mark_the_open_list(client, admin_user):
    client.force_login(admin_user)
    for tab_set in SECTION_TABS.values():
        owners = {ROUTE_ITEMS[tab.url_name] for tab in tab_set.tabs}
        assert len(owners) == 1 and owners <= STAFF_KEYS, tab_set.label
        for tab in tab_set.tabs:
            nav = region(client.get(reverse(tab.url_name)).content, '<nav class="vr-section-tabs"')
            current = re.findall(r'aria-current="page">(.*?)</a>', nav)
            assert current == [tab.label], (tab.url_name, current)
