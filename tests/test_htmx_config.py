"""HTMX configuration that the Content-Security-Policy depends on (core/security_headers.py)."""

import json
import re
from pathlib import Path

from django.conf import settings

DOCUMENT = Path(settings.BASE_DIR) / "templates/layouts/document.html"


def htmx_config():
    meta = re.search(r"""<meta name="htmx-config" content='([^']*)'>""", DOCUMENT.read_text())
    assert meta, "layouts/document.html lost its htmx-config meta"
    return json.loads(meta.group(1))


def test_htmx_never_writes_inline_styles():
    """style-src 'self' blocks style attributes written by script.

    htmx would write them when it settles a swap: by default it copies the old element's class,
    style, width and height onto the new one with setAttribute. Styles set through the CSSOM by
    Bootstrap, DataTables or a resized textarea would then be re-applied as an attribute.
    """
    config = htmx_config()
    assert "style" not in config.get("attributesToSettle", ["style"])
    assert config["includeIndicatorStyles"] is False
    assert config["allowEval"] is False
