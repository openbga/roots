"""Editing surface metadata for progressive enhancement, never authorization policy."""

import re
from dataclasses import dataclass
from functools import cache

from django.urls import URLResolver, get_resolver

# Dedicated editing pages only. Record pages with inline actions remain record pages.
FORM_SURFACES = {
    "receipt:client-create": "lg",
    "receipt:client-edit": "lg",
    "receipt:project-create": "lg",
    "receipt:project-edit": "lg",
    "receipt:contact-create": "lg",
    "receipt:contact-edit": "lg",
    "receipt:order-create": "lg",
    "receipt:order-edit": "lg",
    "receipt:order-add-sample": "xl",
    "receipt:sample-create": "xl",
    "receipt:sample-correct": "xl",
    "receipt:order-correct-reference": "lg",
    "receipt:sample-disposition": "lg",
    "receipt:order-schedule": "lg",
    "receipt:sample-retention": "lg",
    "receipt:sample-disposal-hold": "lg",
    "receipt:sample-dispose": "lg",
    "receipt:subcontract-create": "lg",
    "receipt:subcontract-result-create": "xl",
    "receipt:subcontract-result-review": "lg",
    "receipt:subcontract-cancel": "lg",
    "announcement-create": "lg",
    "announcement-edit": "lg",
    "lab-profile-edit": "lg",
    "lab-logo-edit": "lg",
    "lab-certificate-create": "lg",
    "lab-certificate-edit": "lg",
    "catalog:test-create": "lg",
    "catalog:test-edit": "lg",
    "catalog:method-create": "lg",
    "catalog:method-edit": "lg",
    "catalog:method-new-version": "lg",
    "catalog:analyte-create": "lg",
    "catalog:analyte-edit": "lg",
    "analytical:batch-create": "lg",
    "analytical:batch-add-tests": "xl",
    "analytical:batch-entry": "xl",
    "analytical:batch-import": "xl",
    "analytical:batch-import-mapped": "xl",
    "dataimport:upload": "xl",
    "analytical:import-profile-create": "xl",
    "analytical:import-profile-edit": "xl",
    "analytical:qc-sample-add": "lg",
    "analytical:qc-measure": "xl",
    "analytical:qc-action": "lg",
    "analytical:config-new": "lg",
    "analytical:config-edit": "lg",
    "analytical:preservation-assess": "lg",
    "analytical:prep-create": "lg",
    "analytical:prep-aliquot-add": "lg",
    "analytical:prep-aliquot-cancel": "lg",
    "analytical:prep-start": "lg",
    "analytical:prep-complete": "lg",
    "analytical:mdl-create": "lg",
    "analytical:mdl-import-blanks": "lg",
    "analytical:mdl-exclude": "lg",
    "analytical:mdl-observation-add": "lg",
    "analytical:mdl-calculate": "lg",
    "laboratory:material-create": "lg",
    "laboratory:material-edit": "lg",
    "laboratory:lot-create": "lg",
    "laboratory:lot-edit": "lg",
    "laboratory:lot-use": "lg",
    "laboratory:lot-receive": "lg",
    "laboratory:lot-adjust": "lg",
    "laboratory:lot-retire": "lg",
    "laboratory:movement-reverse": "lg",
    "laboratory:equipment-create": "lg",
    "laboratory:equipment-edit": "lg",
    "laboratory:equipment-status": "lg",
    "laboratory:event-create": "lg",
    "laboratory:event-correct": "lg",
    "laboratory:training-create": "lg",
    "laboratory:training-renew": "lg",
    "laboratory:training-correct": "lg",
    "laboratory:nc-create": "lg",
    "laboratory:nc-edit": "lg",
    "laboratory:nc-note": "lg",
    "laboratory:nc-step": "lg",
    "commercial:quote-create": "lg",
    "commercial:quote-edit": "xl",
    "commercial:price-list-create": "xl",
    "commercial:price-list-edit": "xl",
    "commercial:submission-receive": "xl",
    "client_commercial:submission-create": "lg",
    "client_commercial:submission-edit": "lg",
    "client_commercial:sample-create": "xl",
    "client_commercial:sample-edit": "xl",
    "reporting:report-create": "xl",
    "reporting:report-edit": "xl",
    "reporting:nysdec-project": "lg",
    "reporting:nysdec-test": "lg",
    "reporting:nysdec-qualifier": "lg",
    "reporting:edd-context": "xl",
}


@dataclass(frozen=True)
class FormSurface:
    size: str


def surface_for_request(request):
    """Return presentation metadata for an already resolved request."""
    match = getattr(request, "resolver_match", None)
    size = FORM_SURFACES.get(match.view_name) if match is not None else None
    return FormSurface(size) if size else None


@cache
def form_route_patterns():
    """Return JavaScript-compatible path regexes from Django's canonical routes.

    The current application's converters use regex syntax shared by Python and JS.
    Remove Python's named-group spelling and end anchor before exposing the patterns.
    Callers match URL.pathname, without its query string or fragment.
    """
    result = []

    def visit(resolver, prefix="", namespaces=()):
        for route in resolver.url_patterns:
            regex = route.pattern.regex.pattern.removeprefix("^")
            if isinstance(route, URLResolver):
                child_namespaces = namespaces + ((route.namespace,) if route.namespace else ())
                visit(route, prefix + regex, child_namespaces)
                continue
            name = ":".join((*namespaces, route.name)) if route.name else ""
            if name not in FORM_SURFACES:
                continue
            pattern = re.sub(r"\(\?P<[^>]+>", "(?:", prefix + regex)
            pattern = pattern.replace(r"\Z", "$")
            if not pattern.endswith("$"):
                pattern += "$"
            result.append({"pattern": "^/" + pattern, "size": FORM_SURFACES[name]})

    visit(get_resolver())
    return result
