"""The public brochure is served without intercepting the protected workspace."""

from urllib.parse import urljoin

import pytest


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
    assert "OpenBGA Roots" in html and "AGPL-3.0-only" in html
    assert 'href="https://bgasoft.com"' in html
    assert 'href="https://github.com/openbga/roots"' in html
    for path, content_type in (
        ("/about/assets/site.css", "text/css"),
        ("/about/assets/mark.svg", "image/svg+xml"),
    ):
        asset = client.get(path)
        assert asset.status_code == 200
        assert asset["Content-Type"].startswith(content_type)
        asset.close()


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
