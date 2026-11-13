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

from core.templatetags.ui import COMPONENTS

ROOT = Path(settings.BASE_DIR)

# Print documents rendered to PDF carry their own stylesheet; they are not workspace pages.
DOCUMENTS = {
    "receipt/templates/receipt/pdf/labels.html",
    "templates/acceptance/report.html",  # acceptance evidence: a standalone file, never served
    "reporting/templates/reporting/pdf/report.html",
}


def page_actions(source):
    return "".join(re.findall(r"{% block page_actions %}(.*?){% endblock", source, re.S))


PRIMARY_POST_BUTTON = re.compile(
    r"""{%\s*include\s+["']includes/post_button\.html["'][^%]*\bvariant=["']primary["']"""
)


def primaries(text):
    """Primary buttons written out, and post_button includes that render one."""
    return text.count("btn-primary") + len(PRIMARY_POST_BUTTON.findall(text))


def most_primaries(source):
    """Primary count on the fullest path through {% if %} branches (they exclude each other)."""
    stack, pos = [[0]], 0
    for tag in re.finditer(r"{%\s*(if|elif|else|endif)\b.*?%}", source):
        stack[-1][-1] += primaries(source[pos : tag.start()])
        pos = tag.end()
        if tag.group(1) == "if":
            stack.append([0])
        elif tag.group(1) == "endif" and len(stack) > 1:
            branches = stack.pop()
            stack[-1][-1] += max(branches)
        elif tag.group(1) != "endif":
            stack[-1].append(0)
    stack[-1][-1] += primaries(source[pos:])
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
    # Phase 1 component rules (docs/ui-standards.md section 8). Owners ending in "/" are folders.
    "card-markup": (
        "Panels and table cards only through {% panel %} (includes/panel.html)",
        re.compile(r"""class="[^"]*\bcard(?:-header|-body|-footer|-title)?\b"""),
        {"templates/includes/panel.html", "templates/includes/panel_section.html"},
    ),
    "alert-markup": (
        "Notices only through {% notice %} (includes/notice.html)",
        re.compile(r"""class="[^"]*\balert\b"""),
        {"templates/includes/notice.html"},
    ),
    "list-state": (
        "Empty, no-results and filter-error states only through {% list_results %}",
        re.compile(r"""kind=["']no-results["']"""),
        {"templates/includes/list_results.html", "templates/includes/empty_state.html"},
    ),
    "history-panel": (
        "History only through {% history_panel %} (includes/history_panel.html)",
        re.compile(r"\bvr-timeline\b"),
        {"templates/includes/history_panel.html", "templates/includes/history_item.html"},
    ),
    "fact-row": (
        "Facts only through {% fact %} (includes/fact.html); no hand-written <dt>",
        re.compile(r"<dt\b"),
        {"templates/includes/fact.html"},
    ),
    "page-meta-list": (
        "Key facts under the title only through {% page_meta %} (includes/page_meta.html)",
        re.compile(r"\bvr-page-meta\b"),
        {"templates/includes/page_meta.html"},
    ),
    "raw-input": (
        "Inputs through form fields or the shared includes (row_select, grid_search, select_all)",
        re.compile(r"""<input\b(?![^>]*\btype=["']hidden["'])"""),
        {"templates/includes/", "templates/django/forms/"},
    ),
    "required-note": (
        "The required-fields sentence only through includes/required_note.html",
        re.compile(r"Fields marked"),
        {"templates/includes/required_note.html"},
    ),
    "form-actions": (
        "Form action rows only through includes/form_actions.html",
        re.compile(r"\bvr-form-actions\b"),
        {"templates/includes/form_actions.html"},
    ),
    "component-include": (
        "Shared components render through their {% load ui %} tags, never a plain include: a tag "
        "gives the component a context of its own (tests/test_include_isolation.py)",
        re.compile(
            r"""{%\s*include\s+["']includes/(?:"""
            + "|".join(sorted(map(re.escape, COMPONENTS)))
            + r""")\.html["']"""
        ),
        set(),
    ),
    "colour-utility": (
        "Muted text is .vr-muted; no Bootstrap colour utility app.css does not theme",
        re.compile(
            r"""(?<=[\s"'])(?:text-(?:muted|body-secondary|body-tertiary|primary|secondary|success|danger"""
            r"""|warning|info|light|dark|white)|text-bg-[a-z]+|text-[a-z]+-emphasis|bg-[a-z]+-subtle"""
            r"""|bg-(?:primary|secondary|success|danger|warning|info|dark|body|body-secondary|body-tertiary)"""
            r"""|border-(?:primary|secondary|success|danger|warning|info|light|dark)"""
            r"""|link-(?:primary|secondary|success|danger|warning|info|light|dark|body-emphasis)"""
            r"""|btn-(?:secondary|success|warning|info|light|dark)|btn-outline-(?:success|warning|info|light|dark)"""
            r"""|table-(?:primary|success|warning|info|dark|active))(?=[\s"'])"""
        ),
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
            if name in owners or any(o.endswith("/") and name.startswith(o) for o in owners):
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
    post = '{% include "includes/post_button.html" with action=u label="Issue" variant="primary" %}'
    secondary = '{% include "includes/post_button.html" with action=u label="Withdraw" %}'
    assert most_primaries(f"{post}{secondary}") == 1
    assert most_primaries(f"{post}{{% if b %}}{primary}{{% endif %}}") == 2


def test_shared_includes_render_their_documented_parameters(rf, django_user_model):
    """Render each shared include once with its documented parameters."""
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
    assert 'class="vr-brand" href="/"' in brand
    assert ">Smart Roots</span>" in brand and ">An OpenBGA project</span>" in brand
    portal_brand = render_to_string("includes/brand.html", {"href": "/", "name": "Example Lab"})
    assert ">Example Lab</span>" in portal_brand and "OpenBGA" not in portal_brand


def test_row_select_ignores_the_page_form():
    """An include sees its caller's context; the page's own `form` must not become the attribute."""
    from django import forms
    from django.template import Context, Template

    class PageForm(forms.Form):
        title = forms.CharField()

    page = Template(
        '{% include "includes/row_select.html" with name="samples" value=1 %}'
        '{% include "includes/row_select.html" with name="samples" value=2 form_id="bulk" %}'
    ).render(Context({"form": PageForm()}))
    first, second = page.split("<input", 2)[1:]
    assert "form=" not in first and 'name="title"' not in page
    assert 'form="bulk"' in second
