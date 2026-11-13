"""Page help rendering: static help for error templates, and the [[Label]] marker of help text."""

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from core.page_docs import LABEL, documentation_for_route

register = template.Library()


@register.simple_tag
def page_help(route):
    return documentation_for_route(route)


@register.filter
def help_text(value):
    """Help text as HTML: the whole string escaped first, then each [[Label]] (an exact
    on-screen label, already escaped) wrapped as <span class="vr-help-ui">. No other markup."""
    escaped = escape(value)
    return mark_safe(
        LABEL.sub(lambda match: f'<span class="vr-help-ui">{match.group(1)}</span>', escaped)
    )
