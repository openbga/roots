"""Shared page components (docs/ui-standards.md section 8): every one is a tag of this library.

  {% load ui %}
  {% status label=sample.get_status_display tone="success" plain=True %}
  {% icon name="plus-lg" %}
  {% panel title="Projects" count=projects|length table=True %}
    {% fill actions %}<a class="btn btn-sm btn-outline-secondary" href="...">Add</a>{% endfill %}
    ...body...
  {% endpanel %}
  {% fact label="Phone" value=client.phone %}             one dt/dd row; without value= a block

COMPONENTS below is the one declaration: each tag's template under templates/includes/ (whose
header comment documents it), its keyword parameters, the page variables it is documented to read,
and whether it wraps a body. Every tag renders its template in a context of its own, built from:
  - its parameters: keyword arguments only; an unknown one is a template syntax error, and one the
    caller leaves out is empty ("", as a missing variable renders), never a page variable of the
    same name;
  - its documented reads (`reads`): page variables taken from the caller's context, each only when
    the caller does not pass it as a parameter (the list views put filter_form, q, sort ... in it);
  - request, user and csrf_token.
A block tag's body renders first, in the page's context. {% fill name %} ... {% endfill %},
written directly inside the tag (not inside an {% if %}), renders into the named part
(slots.<name>) instead of the body. The body and each part are stripped of surrounding
whitespace, so an empty body is falsy.
"""

from collections.abc import Callable
from dataclasses import dataclass

from django import template
from django.template.base import token_kwargs
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe
from django.utils.text import slugify

from core.grids import PageSizeForm

register = template.Library()

# The one mapping from a status tone (includes/status.html) to a Bootstrap alert variant and the
# label a notice opens with when the caller gives none.
NOTICE_TONES = {
    "neutral": ("secondary", "Note"),
    "info": ("info", "Note"),
    "progress": ("progress", "In progress"),
    "success": ("success", "Done"),
    "warning": ("warning", "Warning"),
    "danger": ("danger", "Error"),
    "hold": ("hold", "On hold"),
}

# Page variables every component may read.
PASS_THROUGH = ("request", "user", "csrf_token")


class FillNode(template.Node):
    def __init__(self, name, nodelist):
        self.name, self.nodelist = name, nodelist

    def render(self, context):
        # A component collects its direct {% fill %} children; one reached here sits inside
        # another tag ({% if %}, {% for %}) and would vanish silently.
        raise template.TemplateSyntaxError(
            f"{{% fill {self.name} %}} must sit directly inside its component tag; "
            "put conditions inside the fill"
        )


@register.tag
def fill(parser, token):
    bits = token.split_contents()
    if len(bits) != 2:
        raise template.TemplateSyntaxError(
            "{% fill %} takes one part name, e.g. {% fill actions %}"
        )
    nodelist = parser.parse(("endfill",))
    parser.delete_first_token()
    return FillNode(bits[1], nodelist)


@dataclass(frozen=True)
class Component:
    template: str
    params: tuple = ()
    reads: tuple = ()  # documented page variables, taken when not passed as a parameter
    body: str = ""  # "" no body; "block" always a block; "unless:<param>" a block unless given
    derive: Callable | None = None  # extra template values from (values, body, slots)
    caller_templates: tuple = ()  # parameters naming a template rendered in the page's context


class ComponentNode(template.Node):
    def __init__(self, name, spec, kwargs, nodelist):
        self.name, self.spec, self.kwargs, self.nodelist = name, spec, kwargs, nodelist

    def render(self, context):
        spec = self.spec
        values = dict.fromkeys(spec.params, "")
        for name in spec.reads:
            if name not in self.kwargs:
                values[name] = context.get(name, "")
        values.update({name: value.resolve(context) for name, value in self.kwargs.items()})
        engine = context.template.engine
        for name in spec.caller_templates:
            if values[name]:
                values[name] = mark_safe(engine.get_template(values[name]).render(context))
        body, slots = "", {}
        if self.nodelist is not None:
            parts = []
            for node in self.nodelist:
                if isinstance(node, FillNode):
                    slots[node.name] = mark_safe(node.nodelist.render(context).strip())
                else:
                    parts.append(node.render_annotated(context))
            body = mark_safe("".join(parts).strip())
        if spec.body:  # a single component may have its own "body" parameter (empty_state)
            values["body"], values["slots"] = body, slots
        if spec.derive:
            values.update(spec.derive(values, body, slots, self.nodelist is not None))
        for name in PASS_THROUGH:
            values.setdefault(name, context.get(name, ""))
        return engine.get_template(spec.template).render(context.new(values))


def compile_component(name, spec):
    def compile_tag(parser, token):
        bits = token.split_contents()
        kwargs = token_kwargs(bits[1:], parser, support_legacy=False)
        if len(kwargs) != len(bits) - 1:
            raise template.TemplateSyntaxError(
                f"{{% {name} %}} takes keyword arguments only (name=value)"
            )
        unknown = set(kwargs) - set(spec.params)
        if unknown:
            raise template.TemplateSyntaxError(
                f"{{% {name} %}} has no parameter {', '.join(sorted(unknown))}"
            )
        nodelist = None
        unless = spec.body.removeprefix("unless:")
        if spec.body == "block" or (spec.body.startswith("unless:") and unless not in kwargs):
            nodelist = parser.parse((f"end{name}",))
            parser.delete_first_token()
        return ComponentNode(name, spec, kwargs, nodelist)

    return compile_tag


def panel_values(values, body, slots, has_body):
    title = values["title"] or strip_tags(slots.get("title", "")) or "panel"
    return {"heading_id": values["heading_id"] or f"{slugify(title)}-heading"}


def list_results_values(values, body, slots, has_body):
    items = values["items"]
    paginator = getattr(items, "paginator", None)
    has_rows = bool(paginator.count) if paginator is not None else bool(items)
    return {
        "has_rows": has_rows,
        "paginated": paginator is not None and values["paginate"] is not False,
    }


def notice_values(values, body, slots, has_body):
    tone = values["tone"] or "info"
    if tone not in NOTICE_TONES:
        raise template.TemplateSyntaxError(
            f"{{% notice %}} tone must be one of {', '.join(NOTICE_TONES)}"
        )
    variant, label = NOTICE_TONES[tone]
    return {"tone": tone, "variant": variant, "label": values["label"] or label}


def history_panel_values(values, body, slots, has_body):
    title = values["title"] or "History"
    events = values["events"] or []
    if values["newest_first"]:
        events = list(reversed(list(events)))
    return {
        "title": title,
        "events": events,
        "heading_id": values["heading_id"] or f"{slugify(title)}-heading",
    }


def matrix_tag_values(values, body, slots, has_body):
    from receipt.models import MATRIX_ICONS, Sample  # app models load after this library

    matrix = values["matrix"]
    known = matrix in Sample.Matrix.values
    name = values["name"] or (Sample.Matrix(matrix).label if known else matrix)
    source = str(values["source"] or "")
    if source.lower() == str(name).lower():
        source = ""  # "Soil · Soil" says nothing more
    # The visible source drops a leading matrix word ("Wastewater effluent" -> "effluent");
    # the title and the screen-reader text keep the whole value.
    short = source
    if source.lower().startswith(f"{str(name).lower()} "):
        short = source[len(str(name)) + 1 :]
    return {
        "icon": MATRIX_ICONS.get(matrix, ""),
        "data_matrix": matrix if known else "",
        "name": name,
        "source": source,
        "short_source": short,
    }


def due_pill_values(values, body, slots, has_body):
    from core.due import TONES, due_state

    soon = values["soon_days"]
    kind, label = due_state(
        values["due"] or None,
        today=values["today"] or None,
        soon_days=30 if soon == "" else int(soon),
    )
    return {"kind": kind, "label": label, "tone": TONES[kind]}


def form_actions_values(values, body, slots, has_body):
    return {"extra": body}


def fact_values(values, body, slots, has_body):
    value = body if has_body else values["value"]
    blank = value is None or (isinstance(value, str) and not value.strip())
    return {"value": None if blank else value}


I = "includes/"  # noqa: E741
COMPONENTS = {
    # Wrapping components (block tags)
    "panel": Component(
        I + "panel.html",
        ("title", "heading_id", "count", "table", "flush", "hidden_title", "collapsible",
         "open", "class", "id", "body_class"),
        body="block",
        derive=panel_values,
    ),
    "panel_section": Component(I + "panel_section.html", ("rule", "class"), body="block"),
    "list_results": Component(
        I + "list_results.html",
        ("items", "noun", "filtered", "invalid", "clear_url", "paginate", "post_name",
         "empty_title", "empty_body", "action_url", "action_label", "no_results_title",
         "no_results_body", "error_body"),
        body="block",
        derive=list_results_values,
    ),
    "notice": Component(
        I + "notice.html",
        ("tone", "label", "heading", "blocking", "dismissible", "focus", "hidden", "id",
         "class", "hook"),
        body="block",
        derive=notice_values,
    ),
    "form_page": Component(
        I + "form_page.html",
        ("form", "formset", "formset_label", "action", "multipart", "note", "times",
         "no_required_note", "cancel_url", "submit_label", "destructive", "secondary",
         "no_submit", "local", "title", "method", "id", "class"),
        body="block",
    ),
    "form_actions": Component(
        I + "form_actions.html",
        ("cancel_url", "submit_label", "destructive", "secondary", "no_submit"),
        body="block",
        derive=form_actions_values,
    ),
    "history_panel": Component(
        I + "history_panel.html",
        ("events", "newest_first", "title", "heading_id", "count", "class", "empty_title",
         "empty_body"),
        body="block",
        reads=("form_dialog",),
        derive=history_panel_values,
    ),
    "page_meta": Component(I + "page_meta.html", body="block"),
    "more_actions": Component(I + "more_actions.html", ("label",), body="block"),
    "record_layout": Component(I + "record_layout.html", ("aside_label",), body="block"),
    "fact": Component(
        I + "fact.html", ("label", "value", "empty", "class", "wide"), body="unless:value",
        derive=fact_values,
    ),
    # Single components
    "icon": Component(I + "icon.html", ("name", "class")),
    "due_pill": Component(
        I + "due_pill.html", ("due", "soon_days", "today", "plain"), derive=due_pill_values
    ),
    "matrix_tag": Component(
        I + "matrix_tag.html", ("matrix", "name", "source", "size"), derive=matrix_tag_values
    ),
    "status": Component(I + "status.html", ("label", "tone", "plain", "title")),
    "brand": Component(I + "brand.html", ("href", "name", "variant")),
    "breadcrumbs": Component(
        I + "breadcrumbs.html",
        ("crumbs", "section", "section_url", "tab", "tab_url", "record", "record_url", "child",
         "child_url", "current"),
        reads=("page_title",),
    ),
    "confirm_dialog": Component(
        I + "confirm_dialog.html",
        ("id", "label", "object", "title", "body", "action", "hidden_name", "hidden_value",
         "confirm_hook", "cancel_hook", "cancel_label"),
    ),
    "empty_state": Component(
        I + "empty_state.html",
        ("kind", "title", "body", "action_url", "action_label", "clear_url", "invalid"),
        reads=("filter_form",),
    ),
    "filter_chips": Component(I + "filter_chips.html", ("chips",)),
    "form_errors": Component(I + "form_errors.html", ("form", "formset", "formset_label")),
    "form_sections": Component(I + "form_sections.html", ("form",)),
    "required_note": Component(I + "required_note.html", ("note", "times")),
    "field": Component(I + "field.html", ("field", "prefix", "suffix", "no_help", "label")),
    "grid_search": Component(
        I + "grid_search.html", ("value", "placeholder", "label", "name", "id")
    ),
    "grid_toolbar": Component(
        I + "grid_toolbar.html",
        ("label", "q", "placeholder", "search_id", "no_search", "inline", "more", "more_open",
         "active", "clear_url", "grid", "hidden", "invalid", "chips", "sort"),
        reads=("q", "placeholder", "inline", "more", "more_open", "active", "sort",
               "filter_form", "active_filters"),
    ),
    "grid_columns_button": Component(I + "grid_columns_button.html", ("grid",)),
    "sort_header": Component(
        I + "sort_header.html",
        ("label", "asc", "desc", "first", "sort", "class", "col", "locked", "hidden", "priority"),
        reads=("sort",),
    ),
    "pagination": Component(
        I + "pagination.html", ("page_obj", "noun", "filtered", "post_name"),
        reads=("page_obj",),
    ),
    "select_all": Component(I + "select_all.html", ("name", "label", "ready_label")),
    "row_select": Component(
        I + "row_select.html",
        ("name", "value", "label", "form_id", "ready", "checked", "disabled", "id",
         "describedby"),
    ),
    "post_button": Component(
        I + "post_button.html",
        ("action", "label", "variant", "small", "fields", "name", "value", "icon", "id"),
    ),
    "history_item": Component(
        I + "history_item.html",
        ("title", "at", "actor", "by", "reason", "changes", "detail", "extra"),
        caller_templates=("extra",),
    ),
    "page_toc": Component(I + "page_toc.html", ("items", "label")),
    "record_foot": Component(I + "record_foot.html", ("identifier",)),
    "source_footer": Component(I + "source_footer.html", ("css",)),
}  # fmt: skip

for _name, _spec in COMPONENTS.items():
    register.tag(_name, compile_component(_name, _spec))


@register.simple_tag
def page_size_field(per_page, noun):
    """The rows-per-page select of includes/pagination.html, as a bound form field.

    {% page_size_field paginator.per_page noun as field %}{{ field }}  (id "per-page-<noun>")
    """
    form = PageSizeForm(initial={"per_page": per_page})
    widget = form.fields["per_page"].widget
    widget.attrs.update(
        {
            "id": f"per-page-{slugify(noun or 'records')}",
            "class": "form-select form-select-sm w-auto",
        }
    )
    return form["per_page"]
