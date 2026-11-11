"""Printable identity labels. Labels do not assert receipt or preservation for planned work."""

from dataclasses import dataclass

from barcode import Code128
from barcode.writer import SVGWriter
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.safestring import mark_safe

MAX_LABELS = 200


@dataclass(frozen=True)
class Label:
    code: str
    heading: str
    sample_id: str
    detail: str
    collected: str


def barcode_svg(code):
    # Only short printable ASCII identifiers enter the barcode; no customer markup.
    if not code or len(code) > 40 or not all(32 <= ord(char) <= 126 for char in code):
        raise ValidationError("Label identifier must be 1–40 printable ASCII characters.")
    svg = (
        Code128(code, writer=SVGWriter())
        .render(
            {
                "module_width": 0.25,
                "module_height": 9,
                "quiet_zone": 3,
                "font_size": 0,
                "write_text": False,
            }
        )
        .decode("utf-8")
    )
    # Only library-generated SVG is marked safe, never label text or form content.
    return mark_safe(svg[svg.index("<svg") :])  # noqa: S308


def sample_labels(samples, *, kind="containers", copies=1):
    if kind not in {"samples", "containers"} or not 1 <= copies <= 10:
        raise ValidationError("Choose sample or container labels and between 1 and 10 copies.")
    labels = []
    for sample in samples:
        collected = timezone.localtime(sample.collected_at).strftime("%Y-%m-%d %H:%M %Z")
        if kind == "samples":
            labels.append(
                Label(
                    code=sample.lab_id,
                    heading=sample.lab_id,
                    sample_id=sample.client_sample_id,
                    detail=sample.get_matrix_display(),
                    collected=collected,
                )
            )
        else:
            for container in sample.containers.all():
                labels.append(
                    Label(
                        code=f"C-{container.pk:08d}",
                        heading=container.label,
                        sample_id=sample.client_sample_id,
                        detail=f"{sample.lab_id} · {sample.get_matrix_display()}",
                        collected=collected,
                    )
                )
        if len(labels) * copies > MAX_LABELS:
            raise ValidationError(
                "Print at most 200 labels at a time; select an individual sample."
            )
    if not labels:
        raise ValidationError("No received samples or containers are available to label.")
    return [label for label in labels for _ in range(copies)]


def render_label_pdf(labels):
    """Render fixed 4×2 inch identity labels without network/filesystem resource fetching."""
    if not 1 <= len(labels) <= MAX_LABELS:
        raise ValidationError("Print between 1 and 200 labels at a time.")
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    class DenyFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            raise ValueError("Labels cannot load external resources.")

    rows = [{"label": label, "barcode": barcode_svg(label.code)} for label in labels]
    html = render_to_string("receipt/pdf/labels.html", {"rows": rows})
    document = HTML(string=html, url_fetcher=DenyFetcher()).render()
    if len(document.pages) != len(labels):
        raise ValidationError(
            "Label text does not fit the 4 × 2 inch layout. Contact the lab administrator."
        )
    return document.write_pdf()
