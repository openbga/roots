"""PDF rendering of a frozen report snapshot with WeasyPrint.

Only the application template and stylesheet are used, and the data is escaped by Django
templates. `render_html` is a pure function of the snapshot. A report loads two kinds of
resource. The laboratory logo its snapshot names, referenced as `lab-logo:<sha256>`:
`render_pdf` reads those stored bytes (checked against the hash) before rendering and serves
exactly that URL. The stylesheet's fonts, the vendored Overpass files (OFL, static/vendor),
served as `reportfont:<file>`. Every other fetch (network, file, data, another logo) is refused.
A named logo that cannot be loaded, or that the renderer cannot decode, fails the render instead
of printing the report without it. WeasyPrint is imported lazily so the application still
starts where its native libraries are missing; rendering then fails cleanly.
"""

import re
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from core.lab_profile import logo_image

from .models import MAX_PDF_BYTES

LAYOUT = "reporting/pdf/report.html"
NOTE = "reporting/preservation_record.html"
STYLESHEET = Path(__file__).resolve().parent / "static" / "reporting" / "report.css"
LOGO_URL_PREFIX = "lab-logo:"
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
FONT_DIR = Path(settings.BASE_DIR) / "static" / "vendor" / "overpass-5.3.0"
FONTS = ("overpass-latin-wght-normal.woff2", "overpass-latin-ext-wght-normal.woff2")
# A plain scheme: collectstatic leaves "scheme:" URLs in the stylesheet alone.
FONT_URL_PREFIX = "reportfont:"
# Holding statuses a reader must notice. "Met", "No holding rule configured" and a subcontracted
# result's "Not evaluated by this laboratory" are printed in the row without a flag.
HOLDING_FLAGS = {"Exceeded", "Not evaluable"}


class ReportRenderError(Exception):
    pass


def logo_url(logo):
    """Internal URL of a snapshot's logo (`content["lab"]["logo"]`); empty when it has none."""
    sha256 = (logo or {}).get("sha256") or ""
    return f"{LOGO_URL_PREFIX}{sha256}" if SHA256_HEX.fullmatch(sha256) else ""


def _receipt_note(row):
    """The receipt and preparation note of one result, rendered as the report prints it."""
    return mark_safe(render_to_string(NOTE, {"r": row}).strip())  # escaped by the template


def _test_key(item, analyte):
    """What links a result row to its batch QC: the test definition both record, or, in
    snapshots issued before it was recorded, the analyte name (the only link they have).
    The name can differ after an analyte is renamed; the definition reference cannot."""
    definition = item.get("test_definition_id")
    return ("test", definition) if definition is not None else ("name", analyte)


def layout(content):
    """Reading aids derived from a snapshot only: which results need attention and why, and
    whether a sample's receipt note is the same for all of its results (then printed once).

    A result needs attention when it carries a qualifier, a holding status in HOLDING_FLAGS, or
    a recorded QC issue for its test in its analytical or preparation batch. That is the
    relation release itself uses (analytical.services.approval_findings: the batch's
    BatchQC.issues(definition) for the result's test). Results and batch QC are matched by
    batch number and test definition (see _test_key). Nothing is judged here; the flags repeat
    recorded facts."""
    meanings = {q["code"]: q["description"] for q in content.get("qualifiers", [])}
    issues = {}
    for b in content.get("batches", []):
        for analyte, qc in b["qc"].items():
            issues[(b["number"], _test_key(qc, analyte))] = qc.get("issues", [])
    samples, attention = [], []
    for s in content["samples"]:
        results = []
        for r in s["results"]:
            batches = [r.get("batch"), (r.get("preparation") or {}).get("batch")]
            qc_issues = [
                {"batch": number, **issue}
                for number in dict.fromkeys(b for b in batches if b)
                for issue in issues.get((number, _test_key(r, r["analyte"])), [])
            ]
            holding = r.get("holding_status") if r.get("holding_status") in HOLDING_FLAGS else ""
            entry = {
                "r": r,
                "note": _receipt_note(r),
                "qualifiers": [(code, meanings.get(code, "")) for code in r.get("qualifiers", [])],
                "holding_flag": holding,
                "qc_issues": qc_issues,
                "attention": bool(r.get("qualifiers") or holding or qc_issues),
            }
            results.append(entry)
            if entry["attention"]:
                attention.append({"sample": s, **entry})
        flagged = [e for e in results if e["attention"]]
        samples.append(
            {
                "s": s,
                "results": results,
                "shared_note": len({e["note"] for e in results}) == 1,
                "qualifier_flags": sorted({q for e in flagged for q, _ in e["qualifiers"]}),
                "holding_flags": sorted({e["holding_flag"] for e in flagged} - {""}),
                "qc_exception": any(e["qc_issues"] for e in flagged),
            }
        )
    lab_ids = {}  # (batch, analyte) -> lab IDs of this report's results from that batch
    for s in content["samples"]:
        for r in s["results"]:
            for number in (r.get("batch"), (r.get("preparation") or {}).get("batch")):
                if number:
                    lab_ids.setdefault((number, _test_key(r, r["analyte"])), []).append(s["lab_id"])
    batches = [
        {
            "b": b,
            "groups": [
                {
                    "analyte": analyte,
                    "qc": qc,
                    "lab_ids": list(
                        dict.fromkeys(lab_ids.get((b["number"], _test_key(qc, analyte)), []))
                    ),
                }
                for analyte, qc in sorted(b["qc"].items())  # the results' order
            ],
        }
        for b in content.get("batches", [])
    ]
    return {
        "samples": samples,
        "batches": batches,
        "attention": attention,
        "result_count": sum(len(s["results"]) for s in content["samples"]),
    }


def render_html(snapshot, *, preview=False):
    return render_to_string(
        LAYOUT,
        {
            "c": snapshot["content"],
            "release": snapshot.get("release"),
            "sha": snapshot.get("content_sha256"),
            "preview": preview,
            "view": layout(snapshot["content"]),
        },
    )


def _fonts():
    """{url: (bytes, content type)} of the stylesheet's fonts. A missing file fails the render
    instead of printing the report in a fallback face."""
    try:
        return {
            f"{FONT_URL_PREFIX}{name}": ((FONT_DIR / name).read_bytes(), "font/woff2")
            for name in FONTS
        }
    except OSError as exc:
        raise ReportRenderError(
            "The report PDF could not be rendered: its fonts are not installed. Nothing was "
            "released; contact an administrator."
        ) from exc


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


def render_document(snapshot, *, preview=False):
    """The snapshot laid out in pages (a WeasyPrint Document), with only its own resources."""
    logos = _resources(snapshot)
    resources = {**_fonts(), **logos}
    served = set()
    images = {}  # WeasyPrint's image cache: URL -> decoded image, or None when it gave up
    try:
        from weasyprint import CSS, HTML
        from weasyprint.text.fonts import FontConfiguration
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
        fonts = FontConfiguration()  # loads the stylesheet's @font-face rules
        css = CSS(
            string=STYLESHEET.read_text(encoding="utf-8"), url_fetcher=fetcher, font_config=fonts
        )
        document = html.render(stylesheets=[css], cache=images, font_config=fonts)
    except Exception as exc:
        raise ReportRenderError(
            "The report PDF could not be rendered. Nothing was released; "
            "try again or contact an administrator."
        ) from exc
    if not served >= set(logos) or any(images.get(url) is None for url in logos):
        # The layout must have requested and decoded every resource the snapshot names: a
        # report never leaves out its logo silently (WeasyPrint only logs an image it could
        # not load or decode, and prints the page without it).
        raise ReportRenderError(
            "The report PDF could not be rendered: the laboratory logo could not be printed. "
            "Nothing was released; an administrator can upload the logo again or remove it "
            "in Setup → Laboratory profile."
        )
    return document


def render_pdf(snapshot, *, preview=False):
    document = render_document(snapshot, preview=preview)
    try:
        pdf = document.write_pdf()
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
