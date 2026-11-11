"""Static help for error templates, including the database-independent 500 response."""

from django import template

from core.page_docs import documentation_for_route

register = template.Library()


@register.simple_tag
def page_help(route):
    return documentation_for_route(route)
