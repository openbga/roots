"""Template side of core.preferences: the signed-in user's saved presentation settings.

{% load preferences %}
{% client_preferences %}                  JSON for the page's scripts (<script id="vr-preferences">)
{% preference "nav.collapsed" as value %}   one valid value, or None
"""

from django import template
from django.utils.html import json_script

from core import preferences

register = template.Library()


def _prefs(context):
    request = context.get("request")
    return preferences.for_request(request) if request is not None else {}


@register.simple_tag(takes_context=True)
def client_preferences(context):
    shared = {
        key: value for key, value in _prefs(context).items() if preferences.CLIENT_KEYS.match(key)
    }
    return json_script(shared, "vr-preferences")


@register.simple_tag(takes_context=True)
def preference(context, key):
    return _prefs(context).get(key)
