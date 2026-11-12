"""The laboratory profile: the identity a new report prints, its logo and its certificates.

The admin role changes the profile. Every change locks the laboratory row first, the lock a
report release holds through its final recheck, so a release prints exactly the identity that
was previewed. Each change is audited in the same transaction with the values before and
after. Issued reports keep the identity they froze.

The application keeps no scope register. Certificates are printed as the laboratory recorded
them and are never checked against a result; nothing here is a certification or accreditation
claim. A certificate marked for printing is left off a report issued outside its recorded
validity, and `withheld_certificates` names it so the person preparing the report is told.
"""

import hashlib
import io
import os
import threading
from contextlib import contextmanager

from django.core.exceptions import ValidationError
from django.db import transaction
from PIL import Image, ImageFile, ImageOps

from accounts.roles import has_role, require_role

from .audit import record_event, snapshot
from .concurrency import check_original_state
from .models import MAX_LOGO_BYTES, AuditEvent, Lab, LabCertificate, LabLogo

PROFILE_ROLES = ("admin",)
IDENTITY_FIELDS = (
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "phone",
    "email",
    "website",
    "certificate_note",
)
CERTIFICATE_NOTE_LIMIT = 600
MAX_CERTIFICATES = 50
# Printed under the certificate list when the laboratory has not written its own statement.
DEFAULT_CERTIFICATE_NOTE = (
    "Certificates are listed as recorded by the laboratory. Results in this report were not "
    "checked against the scope of any certificate; the issuing authority's current scope "
    "listing applies."
)
# Pillow format name: stored content type. Other decoders are never tried.
LOGO_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg"}
LOGO_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA"})
MIN_LOGO_SIDE = 32
MAX_LOGO_SIDE = 2000
PROFILE_ACTIONS = (
    "lab.profile_changed",
    "lab.logo_changed",
    "lab.logo_removed",
    "lab.certificate_added",
    "lab.certificate_changed",
    "lab.certificate_removed",
)
# state key: (label, status tone)
CERTIFICATE_STATES = {
    "current": ("Current", "success"),
    "not_yet_valid": ("Not yet valid", "info"),
    "expired": ("Expired", "warning"),
}


def can_manage(user):
    return has_role(user, *PROFILE_ROLES)


def _lock_lab():
    lab = Lab.objects.select_for_update().filter(pk=1).first()
    if lab is None:
        raise ValidationError("The laboratory is not set up yet. Run setup_lab first.")
    return lab


def _identity(lab):
    return {name: getattr(lab, name) for name in IDENTITY_FIELDS}


@transaction.atomic
def save_identity(*, actor, values, expected_state=None):
    """Change the address, contact details and certificate statement. Returns (lab, changed)."""
    require_role(actor, *PROFILE_ROLES)
    lab = _lock_lab()
    check_original_state(lab, expected_state)
    before = _identity(lab)
    for name in IDENTITY_FIELDS:
        if name in values:
            setattr(lab, name, (values[name] or "").strip())
    after = _identity(lab)
    if after == before:
        return lab, False
    if len(lab.certificate_note) > CERTIFICATE_NOTE_LIMIT:
        raise ValidationError(
            {"certificate_note": f"Use at most {CERTIFICATE_NOTE_LIMIT} characters."}
        )
    lab.full_clean()
    lab.save(update_fields=list(IDENTITY_FIELDS))
    record_event(actor=actor, action="lab.profile_changed", obj=lab, before=before, after=after)
    return lab, True


_STRICT_DECODING = threading.Lock()


@contextmanager
def _strict_decoding():
    """Have Pillow refuse truncated or damaged image data, whatever the process-wide setting.

    Importing WeasyPrint sets ImageFile.LOAD_TRUNCATED_IMAGES for the whole process, so without
    this a damaged file would be refused before the first PDF render and accepted after it.
    """
    with _STRICT_DECODING:
        tolerant = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        try:
            yield
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = tolerant


def inspect_logo(uploaded):
    """Field values for a LabLogo from an uploaded file. Raises ValidationError.

    The file is untrusted data: only the PNG and JPEG decoders are tried, the pixel size is
    bounded before decoding, and the image is decoded strictly and in full so a damaged file
    is refused here and not printed incomplete on a report. It is then turned and converted
    the way the report renderer does, because the renderer leaves out an image those steps
    reject (reporting.pdf fails such a render). The stored content type comes from the decoded
    format, never from the upload's name or header. The bytes are kept as uploaded; the size
    recorded is the size as printed, after any EXIF orientation.
    """
    name = os.path.basename(getattr(uploaded, "name", "") or "")[:255]
    if uploaded.size > MAX_LOGO_BYTES:
        raise ValidationError(f"The logo is larger than {MAX_LOGO_BYTES // 1024} KiB.")
    data = uploaded.read()
    if not data:
        raise ValidationError("The file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise ValidationError(f"The logo is larger than {MAX_LOGO_BYTES // 1024} KiB.")
    try:
        with (
            _strict_decoding(),
            Image.open(io.BytesIO(data), formats=tuple(LOGO_FORMATS)) as image,
        ):
            image_format, mode = image.format, image.mode
            width, height = image.size
            sized = (
                MIN_LOGO_SIDE <= width <= MAX_LOGO_SIDE and MIN_LOGO_SIDE <= height <= MAX_LOGO_SIDE
            )
            if image_format in LOGO_FORMATS and sized and mode in LOGO_MODES:
                image.load()
                printed = ImageOps.exif_transpose(image) if "exif" in image.info else image
                printed.convert("RGBA")
                width, height = printed.size
    except Exception as exc:  # Pillow raises many types for damaged or oversized images
        raise ValidationError("Upload a PNG or JPEG image. This file could not be read.") from exc
    if image_format not in LOGO_FORMATS:  # for example a multi-picture JPEG
        raise ValidationError("Upload a PNG or JPEG image. This file could not be read.")
    if not sized:
        raise ValidationError(
            f"The logo is {width} × {height} pixels. Each side must be between "
            f"{MIN_LOGO_SIDE} and {MAX_LOGO_SIDE} pixels."
        )
    if mode not in LOGO_MODES:
        raise ValidationError("Save the logo as an RGB or greyscale image and upload it again.")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "image": data,
        "content_type": LOGO_FORMATS[image_format],
        "width": width,
        "height": height,
        "size": len(data),
        "filename": name,
    }


def logo_facts(lab):
    """What identifies the laboratory's current logo, without its bytes; None when there is none."""
    if lab is None or lab.logo_id is None:
        return None
    return (
        LabLogo.objects.filter(pk=lab.logo_id)
        .values("sha256", "content_type", "width", "height")
        .first()
    )


def logo_image(sha256):
    """(bytes, content type) of a stored logo checked against its hash; None when unknown."""
    row = (
        LabLogo.objects.with_image()
        .filter(sha256=sha256)
        .values_list("image", "content_type")
        .first()
    )
    if row is None:
        return None
    data = bytes(row[0])
    if hashlib.sha256(data).hexdigest() != sha256:
        return None
    return data, row[1]


@transaction.atomic
def set_logo(*, actor, uploaded, expected_state=None):
    """Make an uploaded image the laboratory's logo. Returns (lab, changed).

    An image uploaded before is reused, so restoring an earlier logo adds no second copy.
    """
    require_role(actor, *PROFILE_ROLES)
    facts = inspect_logo(uploaded)
    lab = _lock_lab()
    check_original_state(lab, expected_state)
    logo = LabLogo.objects.filter(sha256=facts["sha256"]).first()
    if logo is None:
        logo = LabLogo.objects.create(uploaded_by=actor, **facts)
    if lab.logo_id == logo.pk:
        return lab, False
    before = logo_facts(lab) or {}
    lab.logo = logo
    lab.save(update_fields=["logo"])
    record_event(
        actor=actor, action="lab.logo_changed", obj=lab, before=before, after=logo_facts(lab)
    )
    return lab, True


@transaction.atomic
def remove_logo(*, actor, expected_state=None):
    """Stop printing a logo. The image stays stored for the reports that printed it."""
    require_role(actor, *PROFILE_ROLES)
    lab = _lock_lab()
    check_original_state(lab, expected_state)
    if lab.logo_id is None:
        return lab, False
    before = logo_facts(lab)
    lab.logo = None
    lab.save(update_fields=["logo"])
    record_event(actor=actor, action="lab.logo_removed", obj=lab, before=before, after={})
    return lab, True


def _lock_certificate(pk):
    row = LabCertificate.objects.select_for_update().filter(pk=pk).first()
    if row is None:
        raise ValidationError("This certificate no longer exists.")
    return row


@transaction.atomic
def save_certificate(*, actor, instance, expected_state=None):
    """Add a certificate or change one. Returns (certificate, changed)."""
    require_role(actor, *PROFILE_ROLES)
    _lock_lab()
    old = None
    if instance.pk:
        old = _lock_certificate(instance.pk)
        check_original_state(old, expected_state)
    elif LabCertificate.objects.count() >= MAX_CERTIFICATES:
        raise ValidationError(f"A laboratory can list at most {MAX_CERTIFICATES} certificates.")
    for name in ("authority", "program", "number"):
        setattr(instance, name, (getattr(instance, name) or "").strip())
    before = snapshot(old) if old else {}
    instance.full_clean()
    if old and snapshot(instance) == before:
        return instance, False
    instance.save()
    record_event(
        actor=actor,
        action="lab.certificate_changed" if old else "lab.certificate_added",
        obj=instance,
        before=before,
        after=snapshot(instance),
    )
    return instance, True


@transaction.atomic
def remove_certificate(*, actor, pk, expected_state=None):
    """Remove a certificate from the list and return what it held, which the audit event keeps."""
    require_role(actor, *PROFILE_ROLES)
    _lock_lab()
    row = _lock_certificate(pk)
    check_original_state(row, expected_state)
    removed = snapshot(row)
    record_event(actor=actor, action="lab.certificate_removed", obj=row, before=removed, after={})
    row.delete()
    return removed


def certificate_state(certificate, on):
    """`current`, `not_yet_valid` or `expired` on a date. Both ends are inclusive and a date
    that is not recorded is no bound."""
    if certificate.valid_from and on < certificate.valid_from:
        return "not_yet_valid"
    if certificate.valid_until and on > certificate.valid_until:
        return "expired"
    return "current"


def certificates_with_state(on):
    """Every certificate in list order, each with `state` on that date."""
    rows = list(LabCertificate.objects.all())
    for row in rows:
        row.state = certificate_state(row, on)
    return rows


def withheld_certificates(on):
    """Certificates marked for printing that a report issued on that date leaves off."""
    return [
        row for row in certificates_with_state(on) if row.show_on_reports and row.state != "current"
    ]


def report_identity(lab, *, on):
    """What a report issued on `on` freezes about the laboratory, besides its name.

    Only details that have a value are present, so a report issued before a detail was
    recorded compares equal to one issued without it and an amendment lists no false change.
    The certificate statement is frozen as printed: the laboratory's own, or the default.
    """
    if lab is None:
        return {}
    identity = {}
    if lab.address_lines:
        identity["address"] = lab.address_lines
    for name in ("phone", "email", "website"):
        if getattr(lab, name):
            identity[name] = getattr(lab, name)
    logo = logo_facts(lab)
    if logo:
        identity["logo"] = logo
    certificates = []
    for row in certificates_with_state(on):
        if not row.show_on_reports or row.state != "current":
            continue
        entry = {"authority": row.authority, "number": row.number}
        if row.program:
            entry["program"] = row.program
        if row.valid_from:
            entry["valid_from"] = row.valid_from.isoformat()
        if row.valid_until:
            entry["valid_until"] = row.valid_until.isoformat()
        certificates.append(entry)
    if certificates:
        identity["certificates"] = certificates
        identity["certificate_note"] = lab.certificate_note or DEFAULT_CERTIFICATE_NOTE
    return identity


def history(limit=20):
    """The newest profile changes, newest first: identity, logo and certificates."""
    return (
        AuditEvent.objects.filter(action__in=PROFILE_ACTIONS)
        .select_related("actor")
        .order_by("-occurred_at")[:limit]
    )
