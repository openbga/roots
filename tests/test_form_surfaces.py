"""Route enhancement metadata stays aligned with Django's URL configuration."""

import json
import re
from types import SimpleNamespace
from uuid import UUID

import pytest
from django.urls import URLResolver, get_resolver, resolve, reverse

from core.form_surfaces import FORM_SURFACES, form_route_patterns, surface_for_request


def registered_routes(resolver=None, namespaces=(), converters=None):
    """Collect converter types so reverse() examples track each real route."""
    converters = converters or {}
    for route in (resolver or get_resolver()).url_patterns:
        route_converters = converters | getattr(route.pattern, "converters", {})
        if isinstance(route, URLResolver):
            child_namespaces = namespaces + ((route.namespace,) if route.namespace else ())
            yield from registered_routes(route, child_namespaces, route_converters)
        elif route.name:
            yield ":".join((*namespaces, route.name)), route_converters


@pytest.mark.parametrize("view_name,size", FORM_SURFACES.items())
def test_every_registered_form_resolves_and_matches_browser_pattern(view_name, size):
    routes = dict(registered_routes())
    assert view_name in routes
    kwargs = {}
    for name, converter in routes[view_name].items():
        if converter.regex == "[0-9]+":
            kwargs[name] = 17
        elif converter.__class__.__name__ == "UUIDConverter":
            kwargs[name] = UUID("12345678-1234-1234-1234-123456789abc")
        else:
            kwargs[name] = "example"
    path = reverse(view_name, kwargs=kwargs)
    match = resolve(path)
    assert match.view_name == view_name
    assert surface_for_request(SimpleNamespace(resolver_match=match)).size == size
    patterns = form_route_patterns()
    matches = [entry for entry in patterns if re.fullmatch(entry["pattern"], path)]
    assert matches == [{"pattern": matches[0]["pattern"], "size": size}]
    assert not any(re.fullmatch(entry["pattern"], path + "extra/") for entry in patterns)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/accounts/login/",
        "/admin/",
        "/samples/",
        "/analytical/worklist/",
        "/analytical/batches/17/",
        "/analytical/review/batches/17/",
        "/analytical/results/17/",
        "/analytical/mdl/calculations/17/",
        "/reports/17/preview/",
        "/reports/17/release/",
        "/reports/17/preview.pdf",
        "/reports/17/v1.pdf",
        "/reports/17/v1/edd/nysdec/",
        "/reports/edd/17.zip",
        "/lab/record-retention/",
        "/commercial/quotes/17/issue/",
        "/portal/submissions/17/submit/",
    ],
)
def test_record_action_download_and_account_routes_are_excluded(path):
    assert surface_for_request(SimpleNamespace(resolver_match=resolve(path))) is None
    assert not any(re.fullmatch(entry["pattern"], path) for entry in form_route_patterns())


def test_metadata_is_cached_json_serializable_and_uses_js_regex_syntax():
    patterns = form_route_patterns()
    assert patterns is form_route_patterns()
    assert json.loads(json.dumps(patterns)) == patterns
    assert len(patterns) == len(FORM_SURFACES)
    for entry in patterns:
        assert entry["pattern"].startswith("^/")
        assert entry["pattern"].endswith("$")
        assert "(?P<" not in entry["pattern"]
        assert r"\Z" not in entry["pattern"]
        assert entry["size"] in {"lg", "xl"}


def test_unresolved_request_has_no_surface():
    assert surface_for_request(SimpleNamespace()) is None
    assert surface_for_request(SimpleNamespace(resolver_match=None)) is None
