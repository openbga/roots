"""Retired UI patterns (docs/ui-standards.md section 11).

Each rule is a regular expression run over every first-party template with its comments removed.
Pages are converted area by area, so each rule carries a BASELINE of templates that still offend.
The test fails when a template outside the baseline offends, and when a baseline entry no longer
offends: the baseline only shrinks. When you convert a page, delete its entries here.
"""

import re
from pathlib import Path

from django.apps import apps
from django.conf import settings

ROOT = Path(settings.BASE_DIR)

# Print documents rendered to PDF carry their own stylesheet; they are not workspace pages.
DOCUMENTS = {
    "receipt/templates/receipt/pdf/labels.html",
    "reporting/templates/reporting/pdf/report.html",
}


def page_actions(source):
    return "".join(re.findall(r"{% block page_actions %}(.*?){% endblock", source, re.S))


def most_primaries(source):
    """btn-primary count on the fullest path through {% if %} branches (they exclude each other)."""
    stack, pos = [[0]], 0
    for tag in re.finditer(r"{%\s*(if|elif|else|endif)\b.*?%}", source):
        stack[-1][-1] += source[pos : tag.start()].count("btn-primary")
        pos = tag.end()
        if tag.group(1) == "if":
            stack.append([0])
        elif tag.group(1) == "endif" and len(stack) > 1:
            branches = stack.pop()
            stack[-1][-1] += max(branches)
        elif tag.group(1) != "endif":
            stack[-1].append(0)
    stack[-1][-1] += source[pos:].count("btn-primary")
    return sum(sum(level) for level in stack)


# name: (what the standard asks instead, pattern, the shared include that owns the pattern)
RULES = {
    "inline-style": (
        "No inline style attribute or <style> element: use app.css classes",
        re.compile(r"""\sstyle\s*=|<style\b"""),
        set(),
    ),
    "colour-literal": (
        "No colour literals in templates: use the --vr-* tokens through app.css",
        re.compile(
            r"""(?:fill|stroke|color|bgcolor)\s*=\s*["']?\s*#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(\s*\d"""
        ),
        set(),
    ),
    "banned-utility": (
        "No Bootstrap colour utilities that bypass the tokens",
        re.compile(r"\b(?:bg-white|bg-light|text-dark|text-black|table-light)\b"),
        set(),
    ),
    "pill-navigation": (
        "Section tabs only through includes/section_tabs.html ({% section_tabs %}); no pills",
        re.compile(r"\bnav-(?:pills|tabs|underline)\b"),
        {"templates/includes/section_tabs.html"},
    ),
    "hand-pagination": (
        "Pagination only through includes/pagination.html",
        re.compile(r"""class="[^"]*\bpagination\b"""),
        {"templates/includes/pagination.html"},
    ),
    "status-span": (
        "Status labels only through includes/status.html",
        re.compile(r"""class="[^"]*\bvr-status\b"""),
        {"templates/includes/status.html"},
    ),
    "breadcrumbs": (
        "Breadcrumbs only through includes/breadcrumbs.html",
        re.compile(r"""class="[^"]*\bbreadcrumb(?:-item)?\b"""),
        {"templates/includes/breadcrumbs.html"},
    ),
    "toolbar": (
        "List toolbars only through includes/grid_toolbar.html",
        re.compile(r"""class="[^"]*\b(?:vr-grid-toolbar|vr-filterbar)\b"""),
        {"templates/includes/grid_toolbar.html"},
    ),
    "open-view-link": (
        "A row opens through its identifier link; no Open, View or Details links",
        re.compile(r"<a\b[^>]*>\s*(?:Open|View|Details)\b"),
        set(),
    ),
    "header-primaries": (
        "At most one btn-primary in page_actions",
        None,  # counted below
        set(),
    ),
}

# Templates that offend today (2026-11-09), per rule. Delete entries as pages are converted.
BASELINE = {
    "inline-style": {},
    "pill-navigation": {},
    "hand-pagination": {},
    "status-span": {
        "templates/allauth/elements/badge.html",
    },
    "breadcrumbs": {},
    "toolbar": {},
    "open-view-link": {},
}


def templates():
    dirs = [Path(d) for d in settings.TEMPLATES[0]["DIRS"]]
    dirs += [Path(app.path) / "templates" for app in apps.get_app_configs()]
    for directory in dirs:
        if not directory.is_dir() or ROOT not in directory.parents or ".venv" in directory.parts:
            continue
        for path in sorted(directory.rglob("*.html")):
            yield path.relative_to(ROOT).as_posix(), path.read_text()


def strip_comments(source):
    source = re.sub(r"{% comment %}.*?{% endcomment %}", "", source, flags=re.S)
    source = re.sub(r"{#.*?#}", "", source, flags=re.S)
    return re.sub(r"<!--.*?-->", "", source, flags=re.S)


def offenders():
    found = {rule: set() for rule in RULES}
    for name, source in templates():
        if name in DOCUMENTS:
            continue
        source = strip_comments(source)
        for rule, (_, pattern, owners) in RULES.items():
            if name in owners:
                continue
            if pattern is None:
                offends = most_primaries(page_actions(source)) > 1
            else:
                offends = bool(pattern.search(source))
            if offends:
                found[rule].add(name)
    return found


def test_templates_do_not_add_retired_patterns():
    found = offenders()
    problems = []
    for rule, (advice, _, _) in RULES.items():
        baseline = set(BASELINE.get(rule, ()))
        problems += [f"{rule}: {name} ({advice})" for name in sorted(found[rule] - baseline)]
        problems += [
            f"{rule}: {name} no longer offends; delete it from BASELINE"
            for name in sorted(baseline - found[rule])
        ]
    assert not problems, "\n".join(problems)


def test_the_scanner_sees_the_templates():
    names = {name for name, _ in templates()}
    assert "templates/base.html" in names
    assert "catalog/templates/catalog/test_list.html" in names
    assert len(names) > 150


def test_header_primaries_counts_one_path_through_branches():
    primary = '<a class="btn btn-primary">Go</a>'
    exclusive = f"{{% if a %}}{primary}{{% else %}}{primary}{{% endif %}}"
    both = f"{primary}{{% if b %}}{primary}{{% endif %}}"
    assert most_primaries(exclusive) == 1
    assert most_primaries(both) == 2


def test_shared_includes_render_their_documented_parameters(rf, django_user_model):
    """No page uses the new includes yet; render each once so phase D starts from working parts."""
    from datetime import UTC, datetime

    from django import forms
    from django.template.loader import render_to_string

    class Filters(forms.Form):
        q = forms.CharField(required=False)
        matrix = forms.ChoiceField(choices=[("", "Any"), ("water", "Water")], required=False)
        client = forms.CharField(required=False)

    request = rf.get("/samples/", {"per_page": "50"})
    form = Filters(request.GET)
    actor = django_user_model(username="history-actor", first_name="Ada", last_name="Analyst")
    at = datetime(2026, 11, 9, 14, 5, tzinfo=UTC)

    crumbs = render_to_string(
        "includes/breadcrumbs.html",
        {"section": "Samples", "section_url": "/samples/", "current": "VR-000001"},
    )
    assert '<a href="/samples/">Samples</a>' in crumbs
    assert 'aria-current="page">VR-000001</li>' in crumbs
    trail = render_to_string(
        "includes/breadcrumbs.html",
        {"crumbs": [("Catalog", None), ("Methods", "/m/"), ("Edit", "/x/")]},
    )
    assert '<li class="breadcrumb-item">Catalog</li>' in trail and 'href="/x/"' not in trail

    tabs = render_to_string(
        "includes/section_tabs.html",
        {
            "label": "What to correct",
            "tabs": [{"label": "Identity", "url": "/a/", "current": True}],
        },
    )
    assert 'aria-current="page">Identity</a>' in tabs and "role=" not in tabs

    toc = render_to_string("includes/page_toc.html", {"items": [("release", "Release")]})
    assert '<a href="#release">Release</a>' in toc

    item = render_to_string(
        "includes/history_item.html",
        {
            "title": "Corrected",
            "at": at,
            "actor": actor,
            "by": "Recorded by",
            "reason": "Typo",
            "changes": [("Matrix", "water", "")],
        },
    )
    assert "Recorded by Ada Analyst" in item and "blank" in item and 'datetime="2026-11-09' in item

    toolbar = render_to_string(
        "includes/grid_toolbar.html",
        {
            "request": request,
            "label": "Filter samples",
            "q": "",
            "inline": [form["matrix"]],
            "more": [form["client"]],
            "grid": "receipt.samples",
            "filter_form": form,
            "active_filters": [],
        },
    )
    assert 'aria-label="Filter samples"' in toolbar and ">Search</label>" in toolbar
    assert 'data-vr-columns-for="receipt.samples"' in toolbar and "Clear all" not in toolbar
    assert 'name="per_page" value="50"' in toolbar and "btn-primary" not in toolbar

    brand = render_to_string("includes/brand.html", {"href": "/", "name": ""})
    assert 'class="vr-brand" href="/"' in brand and "OpenBGA Roots" in brand
