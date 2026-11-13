"""The laboratory identity a report prints: frozen into the content only as recorded, part of
the release fingerprint, never changed on an issued version, compared in amendments, and the
logo served to the PDF renderer only from the snapshot's own hash."""

import hashlib
import io
import os
import re
from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from core import lab_profile
from core.models import Lab, LabCertificate, LabLogo
from reporting import content as report_content
from reporting import services
from reporting.content import assemble, describe_changes, fingerprint
from reporting.models import Report, ReportVersion
from reporting.pdf import ReportRenderError, render_html
from reporting.services import StaleReport, release_report

from .conftest import make_user, texts
from .test_release import release


@pytest.fixture
def admin(people):
    return make_user("profile-admin", "admin")


def today(settings):
    return timezone.localdate(timezone=ZoneInfo(settings.TIME_ZONE))


def image(size=(240, 80), color="navy", image_format="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=image_format)
    return SimpleUploadedFile(f"logo.{image_format.lower()}", buffer.getvalue())


def certificate(admin, **fields):
    values = {"authority": "Example State Environmental Laboratory Program", "number": "EX-10001"}
    return lab_profile.save_certificate(actor=admin, instance=LabCertificate(**(values | fields)))[
        0
    ]


def record_profile(admin, settings):
    """A full synthetic profile: address, contact, logo and two printable current certificates."""
    on = today(settings)
    lab_profile.save_identity(
        actor=admin,
        values={
            "address_line1": "100 Example Way",
            "address_line2": "Suite 5",
            "city": "Testville",
            "state": "NY",
            "postal_code": "12345",
            "phone": "555-0100",
            "email": "lab@example.invalid",
            "website": "https://lab.example.invalid",
        },
    )
    lab = lab_profile.set_logo(actor=admin, uploaded=image())[0]
    certificate(
        admin,
        program="Potable and non-potable water",
        valid_from=on - timedelta(days=200),
        valid_until=on + timedelta(days=165),
        show_on_reports=True,
        position=1,
    )
    certificate(
        admin,
        authority="Example Accreditation Body",
        number="EAB-2",
        show_on_reports=True,
        position=2,
    )
    return lab


def test_content_holds_exactly_what_the_laboratory_recorded(world, admin, settings, monkeypatch):
    report = world["report_a"]
    bare = assemble(report)
    assert set(bare.content["lab"]) == {"name", "timezone"}
    # Without a profile the content and fingerprint are those of a report assembled before
    # laboratory identity existed.
    monkeypatch.setattr(report_content, "report_identity", lambda lab, *, on: {})
    assert assemble(report).fingerprint == bare.fingerprint == fingerprint(bare.content)
    monkeypatch.undo()

    lab_profile.save_identity(actor=admin, values={"phone": "555-0100"})
    assert assemble(report).content["lab"] == {**bare.content["lab"], "phone": "555-0100"}

    on = today(settings)
    lab = record_profile(admin, settings)
    certificate(admin, number="NOT-PRINTED", position=0)  # not marked for printing
    certificate(admin, number="EXPIRED", valid_until=on - timedelta(days=1), show_on_reports=True)
    certificate(admin, number="FUTURE", valid_from=on + timedelta(days=1), show_on_reports=True)
    identity = assemble(report).content["lab"]
    assert identity == {
        "name": "Example Environmental Lab",
        "timezone": settings.TIME_ZONE,
        "address": ["100 Example Way", "Suite 5", "Testville, NY 12345"],
        "phone": "555-0100",
        "email": "lab@example.invalid",
        "website": "https://lab.example.invalid",
        "logo": {
            "sha256": lab.logo.sha256,
            "content_type": "image/png",
            "width": 240,
            "height": 80,
        },
        "certificates": [
            {
                "authority": "Example State Environmental Laboratory Program",
                "number": "EX-10001",
                "program": "Potable and non-potable water",
                "valid_from": (on - timedelta(days=200)).isoformat(),
                "valid_until": (on + timedelta(days=165)).isoformat(),
            },
            {"authority": "Example Accreditation Body", "number": "EAB-2"},
        ],
        "certificate_note": lab_profile.DEFAULT_CERTIFICATE_NOTE,
    }


def test_certificate_outside_its_validity_is_left_off_and_named(
    world, people, admin, settings, client
):
    on = today(settings)
    certificate(admin, number="EXPIRED-1", valid_until=on - timedelta(days=3), show_on_reports=True)
    certificate(admin, number="FUTURE-1", valid_from=on + timedelta(days=4), show_on_reports=True)
    certificate(admin, number="UNLISTED-1", valid_until=on - timedelta(days=3))
    report = world["report_a"]
    assembly = assemble(report)
    assert "certificates" not in assembly.content["lab"]
    assert assembly.releasable  # information, never a release blocker

    client.force_login(people["pm"])
    for route in ("preview", "detail"):
        page = client.get(f"/reports/{report.pk}/{'preview/' if route == 'preview' else ''}")
        assert page.status_code == 200
        text = page.content.decode()
        assert 'id="withheld-certificates"' in text
        assert "EXPIRED-1" in text and "FUTURE-1" in text and "UNLISTED-1" not in text
        assert f'validity ended on <time datetime="{on - timedelta(days=3)}">' in text
        assert f'validity starts on <time datetime="{on + timedelta(days=4)}">' in text
        assert "This does not block release" in text
        assert "an administrator can correct them in" in text
        assert f'<a href="{reverse("lab-profile")}">Setup → Laboratory profile</a>' in text

    LabCertificate.objects.filter(number__in=["EXPIRED-1", "FUTURE-1"]).update(
        show_on_reports=False
    )
    assert (
        'id="withheld-certificates"'
        not in client.get(f"/reports/{report.pk}/preview/").content.decode()
    )


def test_profile_change_after_the_preview_makes_the_release_stale(world, people, admin):
    report = world["report_a"]
    previewed = assemble(report, actor=people["approver"]).fingerprint
    lab_profile.set_logo(actor=admin, uploaded=image())
    with pytest.raises(StaleReport, match="changed after your preview"):
        release_report(
            actor=people["approver"], report_id=report.pk, expected_fingerprint=previewed
        )
    assert not ReportVersion.objects.exists()


def test_issued_report_keeps_its_identity_and_amendment_lists_the_change(
    world, people, admin, settings
):
    record_profile(admin, settings)
    report = world["report_a"]
    original = release(report, people["approver"])
    snapshot = deepcopy(original.file.snapshot)
    pdf = bytes(original.file.pdf)
    html = render_html(snapshot)
    assert "Suite 5" in html and f"lab-logo:{snapshot['content']['lab']['logo']['sha256']}" in html
    assert "Laboratory certificates" in html and "EAB-2" in html

    lab_profile.save_identity(actor=admin, values={"address_line2": "Suite 7"})
    lab_profile.set_logo(actor=admin, uploaded=image(color="darkgreen"))
    removed = LabCertificate.objects.get(number="EAB-2")
    lab_profile.remove_certificate(actor=admin, pk=removed.pk)

    issued = ReportVersion.objects.get(pk=original.pk)
    assert issued.file.snapshot == snapshot and bytes(issued.file.pdf) == pdf
    assert render_html(issued.file.snapshot) == html

    # Only the laboratory identity differs: that is a valid amendment.
    amended = release(Report.objects.get(pk=report.pk), people["approver"], reason="New address")
    assert texts(amended.changes) == [
        "Laboratory address changed.",
        "Laboratory logo changed.",
        "Laboratory certificates changed.",
    ]
    new_lab = amended.file.snapshot["content"]["lab"]
    assert new_lab["address"][1] == "Suite 7" and len(new_lab["certificates"]) == 1
    assert amended.file.snapshot["content"]["samples"] == snapshot["content"]["samples"]
    assert ReportVersion.objects.get(pk=original.pk).file.snapshot == snapshot


def test_amendment_compares_identity_parts_present_on_either_side(world, admin, settings):
    legacy = assemble(world["report_a"]).content  # issued before any part was recorded
    assert describe_changes(legacy, deepcopy(legacy)) == []
    record_profile(admin, settings)
    current = assemble(world["report_a"]).content
    assert texts(describe_changes(legacy, current)) == [
        "Laboratory address changed.",
        "Laboratory phone changed.",
        "Laboratory email changed.",
        "Laboratory website changed.",
        "Laboratory logo changed.",
        "Laboratory certificates changed.",
        "Laboratory certificate statement changed.",
    ]
    assert texts(describe_changes(current, legacy))[0] == "Laboratory address changed."


def test_laboratory_entered_markup_is_escaped(world, admin):
    lab_profile.save_identity(
        actor=admin,
        values={
            "address_line1": '<img src="file:///etc/passwd">',
            "phone": "<b>555-0101</b>",
            "certificate_note": '<link rel="stylesheet" href="http://example.invalid/x.css">',
        },
    )
    certificate(admin, authority="<script>alert(1)</script>", show_on_reports=True)
    snapshot = {"content": assemble(world["report_a"]).content, "release": None}
    html = render_html(snapshot, preview=True)
    assert "<img src" not in html and "<link rel" not in html and "<script>" not in html
    assert "&lt;img src=" in html and "&lt;script&gt;" in html and "&lt;b&gt;555-0101" in html


def require_weasyprint():
    try:
        import weasyprint  # noqa: F401
    except OSError as exc:
        if os.environ.get("REQUIRE_WEASYPRINT") == "1":
            raise
        pytest.skip(f"WeasyPrint native libraries unavailable: {str(exc)[:80]}")


def image_sizes(pdf):
    """(width, height) of every image XObject. Image objects are streams, so their dictionaries
    are never inside a compressed object stream. Test logos have no alpha (no soft mask)."""
    sizes = []
    for header in re.findall(rb"\d+ 0 obj\s*<<((?:(?!endobj).)*?)>>\s*stream", pdf, re.S):
        if re.search(rb"/Subtype\s*/Image\b", header):
            width, height = (
                re.search(rb"/%s\s*(\d+)" % k, header)[1] for k in (b"Width", b"Height")
            )
            sizes.append((int(width), int(height)))
    return sizes


def test_real_pdf_embeds_only_the_snapshot_logo(world, people, admin, settings, monkeypatch):
    """Native WeasyPrint render (see test_release.test_real_pdf_render for the environment)."""
    require_weasyprint()
    from reporting import pdf as report_pdf

    other = lab_profile.set_logo(actor=admin, uploaded=image(size=(300, 100), color="maroon"))[0]
    other_sha = other.logo.sha256
    record_profile(admin, settings)
    version = release(world["report_a"], people["approver"])
    snapshot = version.file.snapshot
    assert snapshot["content"]["lab"]["logo"]["sha256"] != other_sha

    pdf = report_pdf.render_pdf(snapshot)
    assert pdf.startswith(b"%PDF") and b"/Subtype /Image" in pdf
    assert image_sizes(pdf) == [(240, 80)]

    # A snapshot naming a logo that is not stored is not rendered without it.
    unknown = deepcopy(snapshot)
    unknown["content"]["lab"]["logo"]["sha256"] = "0" * 64
    with pytest.raises(ReportRenderError, match="logo"):
        report_pdf.render_pdf(unknown)

    # Every other URL in the layout is still refused: another stored logo, a data URL, a file.
    data_png = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
        "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    layout = report_pdf.render_html

    def with_foreign_images(snap, *, preview=False):
        extra = (
            f'<img src="lab-logo:{other_sha}"><img src="{data_png}">'
            f'<img src="file://{report_pdf.STYLESHEET.parent}/missing.png">'
            '<img src="https://example.invalid/logo.png">'
        )
        return layout(snap, preview=preview).replace("</body>", extra + "</body>")

    monkeypatch.setattr(report_pdf, "render_html", with_foreign_images)
    assert image_sizes(report_pdf.render_pdf(snapshot)) == [(240, 80)]


def test_real_pdf_fails_when_the_renderer_cannot_decode_the_logo(world, people, admin, monkeypatch):
    """A stored logo the renderer gives up on fails the render; WeasyPrint alone would log the
    failure and print the report without the image."""
    require_weasyprint()
    from reporting import pdf as report_pdf

    # Stored directly: an upload like this is refused (tests/test_lab_profile.py).
    buffer = io.BytesIO()
    Image.new("RGB", (240, 80), "navy").save(buffer, format="PNG", exif=b"\xff" * 40)
    data = buffer.getvalue()
    logo = LabLogo.objects.create(
        sha256=hashlib.sha256(data).hexdigest(),
        image=data,
        content_type="image/png",
        width=240,
        height=80,
        size=len(data),
        uploaded_by=admin,
    )
    Lab.objects.filter(pk=1).update(logo=logo)
    assembly = assemble(world["report_a"], actor=people["approver"])
    assert assembly.content["lab"]["logo"]["sha256"] == logo.sha256
    snapshot = {"content": assembly.content, "content_sha256": assembly.fingerprint}
    with pytest.raises(ReportRenderError, match="logo could not be printed"):
        report_pdf.render_pdf(snapshot, preview=True)
    monkeypatch.setattr(services, "render_pdf", report_pdf.render_pdf)  # not the suite's stub
    with pytest.raises(ReportRenderError, match="logo could not be printed"):
        release_report(
            actor=people["approver"],
            report_id=world["report_a"].pk,
            expected_fingerprint=assembly.fingerprint,
        )
    assert not ReportVersion.objects.exists()


def test_real_pdf_without_a_profile_loads_nothing(world, people):
    require_weasyprint()
    from reporting import pdf as report_pdf

    version = release(world["report_a"], people["approver"])
    html = render_html(version.file.snapshot)
    assert 'class="letterhead"' not in html and "Laboratory certificates" not in html
    pdf = report_pdf.render_pdf(version.file.snapshot)
    assert pdf.startswith(b"%PDF") and b"/Subtype /Image" not in pdf
