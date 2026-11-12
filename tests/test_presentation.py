"""The public brochure is served without intercepting the protected workspace."""

import re
from pathlib import Path
from urllib.parse import urljoin

import pytest
from django.conf import settings as django_settings

PRESENTATION = Path(django_settings.WHITENOISE_ROOT) / "about"
PUBLISHED_URL = "https://openbga.github.io/smart-roots-lims/"


# Closing a streamed file runs Django's connection clean-up, which may touch a connection left
# open by an earlier test, so database access is allowed although nothing here queries it.
@pytest.mark.django_db
@pytest.mark.parametrize("debug", [True, False])
def test_public_presentation_and_assets_are_served_by_whitenoise(client, settings, debug):
    settings.DEBUG = debug
    settings.SECURE_SSL_REDIRECT = False
    response = client.get("/about/")
    assert response.status_code == 200
    html = b"".join(response.streaming_content).decode()
    assert "Smart Roots LIMS" in html and "AGPL-3.0-only" in html
    assert 'href="https://bgasoft.com"' in html
    assert 'href="https://github.com/openbga/smart-roots-lims"' in html
    for path, content_type in (
        ("/about/assets/site.css", "text/css"),
        ("/about/assets/mark.svg", "image/svg+xml"),
        ("/about/assets/samples-1280.webp", "image/webp"),
        ("/about/assets/og.png", "image/png"),
    ):
        asset = client.get(path)
        assert asset.status_code == 200
        assert asset["Content-Type"].startswith(content_type)
        asset.close()


@pytest.mark.django_db
def test_static_files_are_same_origin_only_and_cannot_be_framed(client):
    for path in ("/about/", "/about/assets/site.css"):
        response = client.get(path)
        assert response["X-Frame-Options"] == "DENY"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in response
        response.close()


def test_presentation_references_exactly_the_assets_it_ships():
    """The page is also published as a static site, where a missing file fails silently."""
    html = (PRESENTATION / "index.html").read_text()
    referenced = set(re.findall(r'(?:src|href)="(assets/[^"]+)"', html))
    for srcset in re.findall(r'srcset="([^"]+)"', html):
        referenced.update(candidate.split()[0] for candidate in srcset.split(","))
    # The link-preview image must be an absolute URL, so it points at the published copy.
    referenced.update(
        url.removeprefix(PUBLISHED_URL)
        for url in re.findall(r'property="og:image" content="([^"]+)"', html)
    )
    shipped = {
        path.relative_to(PRESENTATION).as_posix()
        for path in (PRESENTATION / "assets").iterdir()
        if path.is_file()
    }
    assert referenced == shipped


@pytest.mark.parametrize("path", ["/about", "/about/index.html"])
def test_presentation_redirects_to_its_directory_url(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert urljoin(path, response["Location"]) == "/about/"


def test_public_presentation_does_not_replace_the_authenticated_workspace(client):
    for path in ("/", "/samples/", "/portal/"):
        response = client.get(path)
        assert response.status_code == 302
        assert response["Location"].startswith("/accounts/login/")
