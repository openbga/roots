"""Application navigation: which sidebar or portal item each page marks, and the section tab sets.

This module is the one declaration of the route-to-item mapping (docs/ui-standards.md section 3,
rule 4). Every page beneath a list marks that list's item, so each item names the routes it owns
besides its own list. A route appears under one item at most; routes reached from the user menu
(account pages) mark none. tests/test_navigation.py checks that every HTML route is declared here
or in its own exemption lists, and that each item's label equals its list page's title.

Templates use:
  {% load navigation %}
  {% nav_item "samples" %}            one sidebar item (includes/nav_link.html)
  {% portal_nav_item "portal-home" %}  one client-portal item (same include, portal markup)
  {% section_tabs "catalog" %}         a declared tab set (includes/section_tabs.html)

Staff menu visibility follows work capabilities below; views still enforce record access.
"""

from dataclasses import dataclass

from django import template
from django.core.exceptions import ImproperlyConfigured
from django.urls import NoReverseMatch, reverse

from accounts.roles import RECEIPT_ROLES, ROLES, can_authorize_release, effective_roles
from commercial.permissions import PRICING_ROLES, QUOTE_ROLES
from laboratory.permissions import EQUIPMENT_ROLES, INVENTORY_ROLES

register = template.Library()


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    url_name: str  # the list or overview the item opens; it marks the item too
    icon: str = ""
    routes: tuple[str, ...] = ()  # further view names (record, form, action pages) it marks


@dataclass(frozen=True)
class Tab:
    label: str
    url_name: str


@dataclass(frozen=True)
class TabSet:
    label: str  # accessible name of the <nav>
    tabs: tuple[Tab, ...]


STAFF_NAV = (
    NavItem("dashboard", "Dashboard", "dashboard", "house"),
    # Sales
    NavItem(
        "quotes",
        "Quotes",
        "commercial:quote-list",
        "receipt",
        ("commercial:quote-create", "commercial:quote-detail", "commercial:quote-edit"),
    ),
    NavItem(
        "price-lists",
        "Price lists",
        "commercial:price-list-list",
        "tag",
        ("commercial:price-list-create", "commercial:price-list-edit"),
    ),
    NavItem(
        "client-submissions",
        "Client submissions",
        "commercial:submission-list",
        "inbox",
        ("commercial:submission-detail", "commercial:submission-receive"),
    ),
    # Intake
    NavItem(
        "clients",
        "Clients",
        "receipt:client-list",
        "building",
        (
            "receipt:client-create",
            "receipt:client-detail",
            "receipt:client-edit",
            "receipt:contact-create",
            "receipt:contact-edit",
        ),
    ),
    NavItem(
        "projects",
        "Projects",
        "receipt:project-list",
        "folder2",
        ("receipt:project-create", "receipt:project-edit"),
    ),
    NavItem(
        "orders",
        "Chains of custody",
        "receipt:order-list",
        "clipboard-check",
        (
            "receipt:order-create",
            "receipt:order-detail",
            "receipt:order-add-sample",
            "receipt:order-schedule",
            "receipt:order-correct-reference",
            "receipt:order-edit",
        ),
    ),
    NavItem(
        "samples",
        "Samples",
        "receipt:sample-list",
        "droplet",
        (
            "receipt:sample-create",
            "receipt:sample-detail",
            "receipt:sample-correct",
            "receipt:sample-disposition",
            "analytical:preservation-assess",
        ),
    ),
    NavItem(
        "storage",
        "Sample storage",
        "receipt:storage-list",
        "archive",
        (
            "receipt:sample-storage",
            "receipt:sample-retention",
            "receipt:sample-disposal-hold",
            "receipt:sample-dispose",
        ),
    ),
    # Bench
    NavItem("schedule", "Delivery schedule", "receipt:schedule", "clock-history"),
    NavItem("worklist", "Worklist", "analytical:batch-worklist", "list-check"),
    NavItem(
        "prep",
        "Preparation batches",
        "analytical:prep-list",
        "inbox",
        (
            "analytical:prep-create",
            "analytical:prep-detail",
            "analytical:prep-aliquot-add",
            "analytical:prep-aliquot-cancel",
            "analytical:prep-start",
            "analytical:prep-complete",
        ),
    ),
    NavItem(
        "batches",
        "Batches & QC",
        "analytical:batch-list",
        "layers",
        (
            "analytical:batch-create",
            "analytical:batch-detail",
            "analytical:batch-add-tests",
            "analytical:batch-entry",
            "analytical:batch-import",
            "analytical:batch-import-mapped",
            "analytical:qc-sample-add",
            "analytical:qc-action",
            "analytical:qc-measure",
            "analytical:result-detail",
        ),
    ),
    NavItem(
        "subcontracts",
        "Subcontracted tests",
        "receipt:subcontract-list",
        "truck",
        (
            "receipt:subcontract-create",
            "receipt:subcontract-detail",
            "receipt:subcontract-result-create",
            "receipt:subcontract-result-review",
            "receipt:subcontract-cancel",
        ),
    ),
    NavItem(
        "mdl",
        "MDL studies",
        "analytical:mdl-list",
        "rulers",
        (
            "analytical:mdl-create",
            "analytical:mdl-detail",
            "analytical:mdl-observation-add",
            "analytical:mdl-import-blanks",
            "analytical:mdl-calculate",
            "analytical:mdl-exclude",
            "analytical:mdl-calculation",
        ),
    ),
    NavItem(
        "charts",
        "QC control charts",
        "analytical:chart-list",
        "graph-up",
        ("analytical:chart-detail",),
    ),
    # Release
    NavItem(
        "review",
        "Technical review",
        "analytical:review-list",
        "patch-check",
        ("analytical:review-batch",),
    ),
    NavItem(
        "reports",
        "Reports",
        "reporting:report-list",
        "file-earmark-text",
        (
            "reporting:report-create",
            "reporting:report-detail",
            "reporting:report-edit",
            "reporting:report-preview",
            # NYSDEC EDD pages work on one report version; their trail starts at Reports.
            "reporting:edd-readiness",
            "reporting:edd-preview",
            "reporting:edd-context",
            # Mapping forms reached only from the EDD preview's fix links.
            "reporting:nysdec-project",
            "reporting:nysdec-test",
            "reporting:nysdec-qualifier",
        ),
    ),
    # Laboratory
    NavItem("lab-overview", "Lab overview", "laboratory:overview", "clipboard-data"),
    NavItem(
        "inventory",
        "Inventory",
        "laboratory:lot-list",
        "box-seam",
        (
            "laboratory:lot-create",
            "laboratory:lot-detail",
            "laboratory:lot-edit",
            "laboratory:lot-use",
            "laboratory:lot-receive",
            "laboratory:lot-adjust",
            "laboratory:lot-retire",
            "laboratory:movement-reverse",
            "laboratory:material-list",
            "laboratory:material-create",
            "laboratory:material-detail",
            "laboratory:material-edit",
        ),
    ),
    NavItem(
        "equipment",
        "Equipment",
        "laboratory:equipment-list",
        "tools",
        (
            "laboratory:equipment-create",
            "laboratory:equipment-detail",
            "laboratory:equipment-edit",
            "laboratory:equipment-status",
            "laboratory:event-create",
            "laboratory:event-correct",
        ),
    ),
    NavItem(
        "training",
        "Training",
        "laboratory:training-list",
        "mortarboard",
        (
            "laboratory:training-create",
            "laboratory:training-detail",
            "laboratory:training-renew",
            "laboratory:training-correct",
        ),
    ),
    NavItem(
        "nonconformances",
        "Nonconformances",
        "laboratory:nc-list",
        "exclamation-octagon",
        (
            "laboratory:nc-create",
            "laboratory:nc-detail",
            "laboratory:nc-edit",
            "laboratory:nc-note",
            "laboratory:nc-step",
        ),
    ),
    # Setup
    NavItem(
        "catalog",
        "Catalog",
        "catalog:test-list",
        "journal-text",
        (
            "catalog:test-create",
            "catalog:test-edit",
            "catalog:method-list",
            "catalog:method-create",
            "catalog:method-edit",
            "catalog:method-new-version",
            "catalog:analyte-list",
            "catalog:analyte-create",
            "catalog:analyte-edit",
        ),
    ),
    NavItem(
        "qc-config",
        "QC configuration",
        "analytical:qc-config",
        "sliders",
        ("analytical:config-new", "analytical:config-edit"),
    ),
    NavItem(
        "import-mappings",
        "Import mappings",
        "analytical:import-profile-list",
        "file-earmark-arrow-up",
        ("analytical:import-profile-create", "analytical:import-profile-edit"),
    ),
    NavItem("record-retention", "Record retention", "record-retention", "lock"),
    NavItem(
        "lab-profile",
        "Laboratory profile",
        "lab-profile",
        "info-circle",
        (
            "lab-profile-edit",
            "lab-logo-edit",
            "lab-certificate-create",
            "lab-certificate-edit",
        ),
    ),
    NavItem(
        "announcements",
        "Announcements",
        "announcement-list",
        "megaphone",
        ("announcement-create", "announcement-edit"),
    ),
    # Django admin renders its own chrome, so no shell page marks this item.
    NavItem("accounts-roles", "Accounts & roles", "admin:index", "people"),
)

PORTAL_NAV = (
    NavItem("portal-home", "Home", "portal:home"),
    NavItem("portal-projects", "Projects", "portal:project-list"),
    NavItem(
        "portal-quotes",
        "Quotes",
        "client_commercial:quote-list",
        "",
        ("client_commercial:quote-detail",),
    ),
    NavItem(
        "portal-submissions",
        "Submissions",
        "client_commercial:submission-list",
        "",
        (
            "client_commercial:submission-create",
            "client_commercial:submission-detail",
            "client_commercial:submission-edit",
            "client_commercial:sample-create",
            "client_commercial:sample-edit",
        ),
    ),
    NavItem("portal-samples", "Samples", "portal:sample-list"),
    NavItem("portal-reports", "Reports", "portal:report-list", "", ("portal:report-detail",)),
)

# Work destinations, not a second authorization policy. Staff may still follow related-record
# links to readable pages outside their menu. Admin's operational roles and combined roles are
# expanded by effective_roles; release authority is checked separately on each request.
STAFF_SECTIONS = (
    ("home", "", ("dashboard",)),
    ("sales", "Sales", ("quotes", "price-lists", "client-submissions")),
    ("intake", "Intake", ("clients", "projects", "orders", "samples", "storage")),
    (
        "bench",
        "Bench",
        ("schedule", "worklist", "prep", "batches", "subcontracts", "mdl", "charts"),
    ),
    ("release", "Release", ("review", "reports")),
    (
        "lab",
        "Laboratory",
        ("lab-overview", "inventory", "equipment", "training", "nonconformances"),
    ),
    (
        "setup",
        "Setup",
        (
            "catalog",
            "qc-config",
            "import-mappings",
            "lab-profile",
            "record-retention",
            "announcements",
            "accounts-roles",
        ),
    ),
)
STAFF_MENU_ROLES = {
    "dashboard": ROLES,
    "quotes": QUOTE_ROLES,
    "price-lists": PRICING_ROLES,
    "client-submissions": RECEIPT_ROLES,
    "clients": ("project_manager",),
    "projects": ("project_manager",),
    "orders": RECEIPT_ROLES,
    "samples": (*RECEIPT_ROLES, "reviewer"),  # reviewers assess preservation
    "storage": RECEIPT_ROLES,
    "schedule": RECEIPT_ROLES,
    "worklist": ("analyst",),
    "prep": ("analyst", "reviewer"),  # reviewers also disposition preparation QC
    "batches": ("analyst", "reviewer"),
    "subcontracts": (*RECEIPT_ROLES, "analyst", "reviewer"),
    "mdl": ("analyst", "reviewer"),
    "charts": ("analyst", "reviewer"),
    "review": ("reviewer",),
    "reports": ("project_manager", "approver"),
    "lab-overview": (*INVENTORY_ROLES, *EQUIPMENT_ROLES),
    "inventory": INVENTORY_ROLES,
    "equipment": EQUIPMENT_ROLES,
    "training": ROLES,  # everyone can read their own training
    "nonconformances": ROLES,  # everyone can open, comment and work on assigned records
    "catalog": ("admin",),
    "qc-config": ("admin",),
    "import-mappings": ("analyst",),
    "lab-profile": ("admin",),  # other staff read it from the report pages' certificate notice
    "record-retention": ("admin",),
    "announcements": ("admin",),
    "accounts-roles": (),  # deployment-owner access is separate from lab roles
}


@register.simple_tag(takes_context=True)
def staff_nav_sections(context):
    """Return nonempty menu sections for this account's current work capabilities."""
    user = context["request"].user
    roles = context.get("user_roles")
    if roles is None:
        roles = effective_roles(user)
    visible = {key for key, allowed in STAFF_MENU_ROLES.items() if roles.intersection(allowed)}
    if roles.intersection(ROLES) and "reports" not in visible and can_authorize_release(user):
        visible.add("reports")
    if (
        user.is_authenticated
        and user.is_active
        and user.client_id is None
        and user.is_staff
        and user.is_superuser
    ):
        visible.add("accounts-roles")
    return [
        {"key": key, "label": label, "items": items}
        for key, label, keys in STAFF_SECTIONS
        if (items := [item for item in keys if item in visible])
    ]


# Pages reached from the user menu: they render inside the user's shell and mark no item.
ACCOUNT_ROUTES = frozenset(
    {
        "account_change_password",
        "account_set_password",
        "account_email",
        "account_logout",
        "mfa_index",
        "mfa_activate_totp",
        "mfa_deactivate_totp",
        "mfa_view_recovery_codes",
        "mfa_generate_recovery_codes",
    }
)

# Section tabs: one sidebar item whose content is two to six sibling lists (standard section 3,
# rule 2). Every list in a set marks the same sidebar item.
SECTION_TABS = {
    "catalog": TabSet(
        "Catalog sections",
        (
            Tab("Tests", "catalog:test-list"),
            Tab("Methods", "catalog:method-list"),
            Tab("Analytes", "catalog:analyte-list"),
        ),
    ),
    "inventory": TabSet(
        "Inventory sections",
        (Tab("Lots", "laboratory:lot-list"), Tab("Materials", "laboratory:material-list")),
    ),
}

# Routes whose page belongs to another item depending on the record it shows. The route is
# declared under its usual item above; its view may name one of the alternatives listed here as
# "nav_item" in the template context (QC pages of a preparation batch mark Preparation batches).
# Any other value is ignored.
SHARED_ROUTES = {
    "analytical:qc-sample-add": frozenset({"prep"}),
    "analytical:qc-measure": frozenset({"prep"}),
    "analytical:qc-action": frozenset({"prep"}),
}

ITEMS = {item.key: item for item in STAFF_NAV + PORTAL_NAV}


def _route_items():
    mapping = {}
    for item in STAFF_NAV + PORTAL_NAV:
        for route in (item.url_name, *item.routes):
            if route in mapping:
                raise ImproperlyConfigured(
                    f"Route {route!r} marks both {mapping[route]!r} and {item.key!r}."
                )
            mapping[route] = item.key
    if len(ITEMS) != len(STAFF_NAV) + len(PORTAL_NAV):
        raise ImproperlyConfigured("Navigation item keys must be unique.")
    return mapping


ROUTE_ITEMS = _route_items()


def current_item_key(request, chosen=None):
    """The key of the item the request's page marks, or None (account, error, unmapped).

    `chosen` is the view's "nav_item" context value; it counts only for a declared shared route.
    """
    match = getattr(request, "resolver_match", None)
    if not match:
        return None
    if chosen in SHARED_ROUTES.get(match.view_name, ()):
        return chosen
    return ROUTE_ITEMS.get(match.view_name)


def _url(url_name):
    try:
        return reverse(url_name)
    except NoReverseMatch:  # not mounted: the item renders as "Planned"
        return ""


def _item_context(context, key, portal):
    item = ITEMS[key]
    request = context.get("request")
    return {
        "url": _url(item.url_name),
        "label": item.label,
        "icon": item.icon,
        "current": request is not None
        and current_item_key(request, context.get("nav_item")) == key,
        "portal": portal,
    }


@register.inclusion_tag("includes/nav_link.html", takes_context=True)
def nav_item(context, key):
    """One staff sidebar item, marked current when the page belongs to it."""
    return _item_context(context, key, portal=False)


@register.inclusion_tag("includes/nav_link.html", takes_context=True)
def portal_nav_item(context, key):
    """One client-portal top-bar item, marked current when the page belongs to it."""
    return _item_context(context, key, portal=True)


@register.inclusion_tag("includes/section_tabs.html", takes_context=True)
def section_tabs(context, name):
    """A declared tab set rendered through includes/section_tabs.html."""
    tab_set = SECTION_TABS[name]
    match = getattr(context.get("request"), "resolver_match", None)
    view_name = match.view_name if match else None
    return {
        "label": tab_set.label,
        "tabs": [
            {"label": tab.label, "url": _url(tab.url_name), "current": tab.url_name == view_name}
            for tab in tab_set.tabs
        ],
    }
