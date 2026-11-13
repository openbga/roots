"""Per-user presentation preferences (core/preferences.py): kept on the server, never shared."""

import json

import pytest
from django.urls import reverse

from core.models import UserPreference
from core.preferences import clean

LAYOUT = {"order": ["client", "lab_id"], "hidden": ["project"]}


def post(client, key, value):
    return client.post(
        reverse("preference"),
        json.dumps({"key": key, "value": value}),
        content_type="application/json",
    )


def page_prefs(client, path):
    """The preferences a page hands to its scripts ({% client_preferences %})."""
    response = client.get(path)
    html = response.content.decode()
    start = html.index('<script id="vr-preferences" type="application/json">') + len(
        '<script id="vr-preferences" type="application/json">'
    )
    return json.loads(html[start : html.index("</script>", start)])


@pytest.mark.django_db
def test_grid_layout_is_saved_per_user_and_isolated(client, staff):
    first, second = staff("analyst-a", "analyst"), staff("analyst-b", "analyst")
    client.force_login(first)
    assert post(client, "grid:receipt.samples:v3", LAYOUT).status_code == 204
    assert page_prefs(client, reverse("receipt:sample-list")) == {"grid:receipt.samples:v3": LAYOUT}
    client.force_login(second)
    assert page_prefs(client, reverse("receipt:sample-list")) == {}  # defaults for everyone else
    client.force_login(first)
    assert post(client, "grid:receipt.samples:v3", None).status_code == 204  # reset
    assert not UserPreference.objects.filter(user=first).exists()


@pytest.mark.django_db
def test_bad_values_are_refused_and_stored_junk_falls_back(client, staff):
    user = staff("custodian-a", "custodian")
    client.force_login(user)
    assert post(client, "grid:receipt.samples:v3", {"order": "lab_id"}).status_code == 400
    assert post(client, "grid:x:v1", {"order": ["<b>"], "hidden": []}).status_code == 400
    assert (
        post(client, "page:receipt:sample-list", {"per_page": 50}).status_code == 400
    )  # server-set
    assert post(client, "theme", "dark").status_code == 400
    assert (
        client.post(reverse("preference"), "not json", content_type="text/plain").status_code == 400
    )
    assert client.get(reverse("preference")).status_code == 405
    # Something stored in an older format is ignored, not trusted (read at the next sign-in).
    UserPreference.objects.create(user=user, key="grid:receipt.samples:v3", value=["lab_id"])
    UserPreference.objects.create(user=user, key="page:receipt:sample-list", value={"per_page": 7})
    client.logout()
    client.force_login(user)
    assert page_prefs(client, reverse("receipt:sample-list")) == {}
    response = client.get(reverse("receipt:sample-list"))
    assert response.context["page_obj"].paginator.per_page == 25


def test_clean_accepts_only_the_declared_shapes():
    assert clean("grid:a.b:v1", LAYOUT) == LAYOUT
    assert clean("grid:a.b:v1", {**LAYOUT, "extra": 1}) is None
    assert clean("page:receipt:sample-list", {"per_page": 100, "sort": "-received"}) == {
        "per_page": 100,
        "sort": "-received",
    }
    assert clean("page:receipt:sample-list", {"per_page": True}) is None
    assert clean("page:receipt:sample-list", {"sort": "x; drop"}) is None
    assert clean("nav.collapsed", True) is True
    assert clean("nav.collapsed", "yes") is None


@pytest.mark.django_db
def test_page_size_and_sort_persist_per_user_and_page(client, staff):
    user, other = staff("custodian-b", "custodian"), staff("custodian-c", "custodian")
    client.force_login(user)
    url = reverse("receipt:sample-list")
    assert client.get(url).context["page_obj"].paginator.per_page == 25  # default
    client.get(url, {"per_page": 50, "sort": "lab_id"})
    response = client.get(url)  # no parameters: the saved ones apply
    assert response.context["page_obj"].paginator.per_page == 50
    assert response.context["request"].GET["sort"] == "lab_id"
    assert client.get(url, {"per_page": 100}).context["page_obj"].paginator.per_page == 100
    assert client.get(url).context["page_obj"].paginator.per_page == 100
    client.get(url, {"per_page": 9})  # invalid: refused by the view, never saved
    assert UserPreference.objects.get(user=user, key="page:receipt:sample-list").value == {
        "per_page": 100,
        "sort": "lab_id",
    }
    client.force_login(other)
    assert client.get(url).context["page_obj"].paginator.per_page == 25


@pytest.mark.django_db
def test_sidebar_state_and_anonymous_requests(client, staff):
    assert post(client, "nav.collapsed", True).status_code == 302  # sign in first
    user = staff("reviewer-a", "reviewer")
    client.force_login(user)
    assert post(client, "nav.collapsed", True).status_code == 204
    assert page_prefs(client, reverse("dashboard")) == {"nav.collapsed": True}


@pytest.mark.django_db
def test_settings_are_read_once_per_sign_in(client, staff):
    user = staff("custodian-d", "custodian")
    client.force_login(user)
    assert post(client, "nav.collapsed", True).status_code == 204
    client.get(reverse("dashboard"))
    UserPreference.objects.filter(user=user).delete()  # only the session copy remains
    assert page_prefs(client, reverse("dashboard")) == {"nav.collapsed": True}
    client.logout()
    client.force_login(user)  # a new sign-in reads the database again
    assert page_prefs(client, reverse("dashboard")) == {}
