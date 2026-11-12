"""Announcements showing now for whoever is looking at the page.

  {% load announcements %}{% announcements %}
  {% announcements placement="aside" %}   sign-in page, beside the form

Used on the sign-in page, the staff dashboard and the client portal home page. The audience is
taken from the request's account in core.announcements, not from the page that renders the tag.
"""

from django import template

from core.announcements import announcements_for

register = template.Library()


@register.inclusion_tag("includes/announcements.html", takes_context=True)
def announcements(context, placement=""):
    """placement="aside": the sign-in page's box beside the form, which keeps the form's height and
    scrolls inside (layouts/entrance.html, entrance_aside)."""
    user = getattr(context.get("request"), "user", None)
    return {
        "announcements": list(announcements_for(user)) if user is not None else [],
        "placement": placement,
    }
