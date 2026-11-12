"""PDF rendering of a frozen report snapshot with WeasyPrint.

Only the application template and stylesheet are used, and the data is escaped by Django
templates. `render_html` is a pure function of the snapshot. The one resource a report may load
is the laboratory logo its snapshot names, referenced as `lab-logo:<sha256>`: `render_pdf` reads
those stored bytes (checked against the hash) before rendering and serves exactly that URL;
every other fetch (network, file, data, another logo) is refused. A named logo that cannot be
loaded, or that the renderer cannot decode, fails the render instead of printing the report
without it. WeasyPrint is imported lazily so the application still starts where its native
libraries are missing; rendering then fails cleanly.
"""

import re
from pathlib import Path

from django.template.loader import render_to_string

from core.lab_profile import logo_image

from .models import MAX_PDF_BYTES

LAYOUT = "reporting/pdf/report.html"
STYLESHEET = Path(__file__).resolve().parent / "static" / "reporting" / "report.css"
LOGO_URL_PREFIX = "lab-logo:"
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class ReportRenderError(Exception):
    pass


def logo_url(logo):
    """Internal URL of a snapshot's logo (`content["lab"]["logo"]`); empty when it has none."""
    sha256 = (logo or {}).get("sha256") or ""
    return f"{LOGO_URL_PREFIX}{sha256}" if SHA256_HEX.fullmatch(sha256) else ""


def render_html(snapshot, *, preview=False):
    return render_to_string(
        LAYOUT,
        {
            "c": snapshot["content"],
            "release": snapshot.get("release"),
            "sha": snapshot.get("content_sha256"),
            "preview": preview,
        },
    )


def _resources(snapshot):
    """{url: (bytes, content type)} the snapshot's PDF may load: its logo, if it names one."""
    logo = snapshot["content"].get("lab", {}).get("logo")
    if not logo:
        return {}
    url = logo_url(logo)
    found = logo_image(logo["sha256"]) if url else None
    if found is None:
        raise ReportRenderError(
            "The report PDF could not be rendered: the laboratory logo this report prints is "
            "not stored. Nothing was released; contact an administrator."
        )
    return {url: found}


def render_pdf(snapshot, *, preview=False):
    resources = _resources(snapshot)
    served = set()
    images = {}  # WeasyPrint's image cache: URL -> decoded image, or None when it gave up
    try:
        from weasyprint import CSS, HTML
        from weasyprint.urls import URLFetcher, URLFetcherResponse

        class SnapshotFetcher(URLFetcher):
            """Serves the snapshot's own resources by exact URL and refuses everything else."""

            def fetch(self, url, headers=None):
                if url not in resources:
                    raise ValueError("Reports cannot load external resources.")
                data, content_type = resources[url]
                served.add(url)
                return URLFetcherResponse(url, data, {"Content-Type": content_type})

        fetcher = SnapshotFetcher()
        html = HTML(
            string=render_html(snapshot, preview=preview), base_url=None, url_fetcher=fetcher
        )
        css = CSS(string=STYLESHEET.read_text(encoding="utf-8"), url_fetcher=fetcher)
        pdf = html.write_pdf(stylesheets=[css], cache=images)
    except Exception as exc:
        raise ReportRenderError(
            "The report PDF could not be rendered. Nothing was released; "
            "try again or contact an administrator."
        ) from exc
    if served != set(resources) or any(images.get(url) is None for url in resources):
        # The layout must have requested and decoded every resource the snapshot names: a
        # report never leaves out its logo silently (WeasyPrint only logs an image it could
        # not load or decode, and prints the page without it).
        raise ReportRenderError(
            "The report PDF could not be rendered: the laboratory logo could not be printed. "
            "Nothing was released; an administrator can upload the logo again or remove it "
            "in Setup → Laboratory profile."
        )
    if not pdf or len(pdf) > MAX_PDF_BYTES:
        raise ReportRenderError(
            f"The rendered PDF is empty or larger than "
            f"{MAX_PDF_BYTES // (1024 * 1024)} MiB. Nothing was released; "
            "split the report into fewer samples."
        )
    return pdf
