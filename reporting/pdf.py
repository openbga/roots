"""PDF rendering of a frozen report snapshot with WeasyPrint.

Only the application template and stylesheet are used; every URL fetch (network, file, data)
is refused, and the data is escaped by Django templates. WeasyPrint is imported lazily so the
application still starts where its native libraries are missing; rendering then fails cleanly.
"""

from pathlib import Path

from django.template.loader import render_to_string

from .models import MAX_PDF_BYTES

LAYOUT = "reporting/pdf/report.html"
STYLESHEET = Path(__file__).resolve().parent / "static" / "reporting" / "report.css"


class ReportRenderError(Exception):
    pass


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


def render_pdf(snapshot, *, preview=False):
    try:
        from weasyprint import CSS, HTML
        from weasyprint.urls import URLFetcher

        class DenyFetcher(URLFetcher):
            def fetch(self, url, headers=None):
                raise ValueError("Reports cannot load external resources.")

        fetcher = DenyFetcher()
        html = HTML(
            string=render_html(snapshot, preview=preview), base_url=None, url_fetcher=fetcher
        )
        css = CSS(string=STYLESHEET.read_text(encoding="utf-8"), url_fetcher=fetcher)
        pdf = html.write_pdf(stylesheets=[css])
    except Exception as exc:
        raise ReportRenderError(
            "The report PDF could not be rendered. Nothing was released; "
            "try again or contact an administrator."
        ) from exc
    if not pdf or len(pdf) > MAX_PDF_BYTES:
        raise ReportRenderError(
            f"The rendered PDF is empty or larger than "
            f"{MAX_PDF_BYTES // (1024 * 1024)} MiB. Nothing was released; "
            "split the report into fewer samples."
        )
    return pdf
