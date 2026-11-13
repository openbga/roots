"""Shared components read only what their caller passes (docs/ui-standards.md section 8).

A Django {% include %} sees its caller's whole context, so an optional parameter the caller left
out was silently filled by any page variable of the same name: a page's `form` once became a
checkbox's form attribute. Every shared component is therefore a `ui` tag
(core/templatetags/ui.py) that renders in a context of its own: its keyword parameters (an omitted
one is empty, never a page variable), its documented reads (breadcrumbs' page_title, empty_state's
filter_form, grid_toolbar's q, sort, filter_form ..., sort_header's sort, pagination's page_obj),
and request, user and csrf_token.

Each component below is rendered with only its explicit arguments, once in a clean context and
once with a decoy page variable for each generic name pages use; the two renderings must be
identical. KNOWN_LEAKS stays empty: a component that starts reading a page variable fails here.
"""

import re

import pytest
from django.template import Context, Template

DECOY_NAMES = [
    "form", "formset", "formset_label", "class", "css", "title", "heading", "heading_id", "items",
    "label", "name", "value", "id", "note", "times", "icon", "small", "checked", "disabled",
    "ready", "describedby", "form_id", "action", "fields", "variant", "field", "body", "kind",
    "tone", "plain", "count", "url", "href", "hidden", "focus", "dismissible", "blocking", "empty",
    "record", "record_url", "current", "section", "section_url", "tab", "tab_url", "crumbs",
    "cancel_url", "submit_label", "destructive", "secondary", "no_submit", "extra", "multipart",
    "object", "hidden_name", "hidden_value", "chips", "grid", "invalid", "clear_url",
    "action_url", "action_label", "noun", "filtered", "post_name", "paginate", "actor", "at",
    "by", "reason", "changes", "detail", "rule", "table", "flush", "collapsible", "body_class",
    "events", "newest_first", "empty_title", "empty_body", "placeholder", "search_id", "prefix",
    "suffix", "ready_label", "identifier", "portal", "hook", "slots",
]  # fmt: skip


def rendered(template, context):
    """The output with the time of rendering ({% now %} in record_foot) masked."""
    return re.sub(r'datetime="[^"]*"', 'datetime="now"', template.render(context))


def page_context(request, values):
    """The page's context as an include sees it ({% querystring %} reads context.request)."""
    context = Context(values)
    context.request = request
    return context


class Decoy:
    """A page variable of any shape: truthy, printable, iterable, with every attribute."""

    def __str__(self):
        return "DECOY"

    __html__ = __str__

    def __bool__(self):
        return True

    def __len__(self):
        return 1

    def __iter__(self):
        return iter([self])

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self

    def __getitem__(self, key):
        return self

    def __call__(self, *args, **kwargs):
        return self


# Each include or block tag with the arguments a typical caller passes explicitly.
CASES = {
    "brand": '{% load ui %}{% brand href="/" %}',
    "breadcrumbs": '{% load ui %}{% breadcrumbs section="Samples" '
    'section_url="/s/" current="VR-1" %}',
    "confirm_dialog": '{% load ui %}{% confirm_dialog id="d" label="Close batch" '
    'body="Closing is final." action="/close/" %}',
    "empty_state": '{% load ui %}{% empty_state kind="empty" title="Nothing yet" invalid=False %}',
    "filter_chips": "{% load ui %}{% filter_chips chips=no_chips %}",
    "form_actions": '{% load ui %}{% form_actions submit_label="Save" %}{% endform_actions %}',
    "form_errors": "{% load ui %}{% form_errors form=real_form formset=None %}",
    "form_sections": "{% load ui %}{% form_sections form=real_form %}",
    "grid_search": '{% load ui %}{% grid_search value="" %}',
    "history_item": '{% load ui %}{% history_item title="Received" at=when %}',
    "icon": '{% load ui %}{% icon name="x" %}',
    "page_toc": "{% load ui %}{% page_toc items=toc %}",
    "post_button": '{% load ui %}{% post_button action="/go/" label="Issue" %}',
    "record_foot": '{% load ui %}{% record_foot identifier="VR-1" %}',
    "required_note": "{% load ui %}{% required_note %}",
    "row_select": '{% load ui %}{% row_select name="samples" value=1 %}',
    "select_all": '{% load ui %}{% select_all name="samples" label="All" %}',
    "sort_header": '{% load ui %}{% sort_header label="Lab ID" asc="lab_id" '
    'col="lab" sort="lab_id" %}',
    "source_footer": "{% load ui %}{% source_footer %}",
    "status": '{% load ui %}{% status label="Received" tone="success" %}',
    "panel": '{% load ui %}{% panel title="Samples" %}Body{% endpanel %}',
    "notice": '{% load ui %}{% notice tone="warning" %}Check this.{% endnotice %}',
    "fact": '{% load ui %}<dl>{% fact label="Matrix" value="Water" %}</dl>',
    "page_meta": "{% load ui %}{% page_meta %}<li>One</li>{% endpage_meta %}",
    "history_panel": "{% load ui %}{% history_panel events=no_events %}{% endhistory_panel %}",
    "form_page": '{% load ui %}{% form_page form=real_form submit_label="Save" %}'
    "{% endform_page %}",
    "list_results": '{% load ui %}{% list_results items=no_events noun="samples" %}'
    "<table></table>{% endlist_results %}",
}


# Names each include still takes from the page when the caller omits them. Delete as fixed.
KNOWN_LEAKS = {}


def leaks(template, request, given):
    """Decoy names that change the output (or break it) when present in the page's context."""
    clean = rendered(template, page_context(request, given))
    found = set()
    for name in DECOY_NAMES:
        try:
            if rendered(template, page_context(request, {name: Decoy(), **given})) != clean:
                found.add(name)
        except Exception:  # a decoy the include cannot even render is a leak too
            found.add(name)
    return found


@pytest.mark.parametrize("case", sorted(CASES))
def test_include_ignores_page_variables_it_was_not_given(case, rf, django_user_model):
    from datetime import UTC, datetime

    from django import forms

    class RealForm(forms.Form):
        matrix = forms.CharField(required=False)

    request = rf.get("/samples/")
    request.user = django_user_model(username="isolation", first_name="Ada", last_name="Analyst")
    given = {
        "request": request,
        "user": request.user,
        "real_form": RealForm(),
        "no_chips": [],
        "no_events": [],
        "toc": [("samples", "Samples")],
        "when": datetime(2026, 11, 9, 14, 5, tzinfo=UTC),
    }
    found = leaks(Template(CASES[case]), request, given)
    known = KNOWN_LEAKS.get(case, set())
    assert not found - known, f"{case} reads page variables not given: {sorted(found - known)}"
    assert not known - found, f"{case} no longer leaks {sorted(known - found)}: update KNOWN_LEAKS"


def test_decoy_names_are_unique():
    assert len(DECOY_NAMES) == len(set(DECOY_NAMES))
