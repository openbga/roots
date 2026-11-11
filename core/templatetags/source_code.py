"""Link to the corresponding source (AGPL-3.0 section 13), configured by SOURCE_CODE_URL.

A tag rather than a context processor so the request-less 500 page can show it too.
"""

from django import template
from django.conf import settings

register = template.Library()


@register.simple_tag
def source_code_url():
    return settings.SOURCE_CODE_URL
