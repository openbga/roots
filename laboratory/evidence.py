"""Bounded evidence attachments: validation on the way in, authorized download on the way out.

Files are untrusted data. The stored content type comes from a fixed extension map confirmed by
the file's leading bytes, never from the client-supplied header; downloads are always attachments.
"""

import hashlib
import os

from django import forms
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils.http import content_disposition_header

MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
ALLOWED = {
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".txt": ("text/plain; charset=utf-8", None),
    ".csv": ("text/csv; charset=utf-8", None),
}


def read_evidence(uploaded):
    """Return the model field values for an uploaded file, or {} when none. Raises ValueError."""
    if not uploaded:
        return {}
    name = os.path.basename(uploaded.name or "")[:255]
    extension = os.path.splitext(name)[1].lower()
    if extension not in ALLOWED:
        raise ValueError("Attach a PDF, PNG, JPEG, TXT or CSV file.")
    if uploaded.size > MAX_EVIDENCE_BYTES:
        raise ValueError("The file is larger than 10 MiB.")
    data = uploaded.read()
    if not data:
        raise ValueError("The file is empty.")
    content_type, signatures = ALLOWED[extension]
    if signatures and not data.startswith(signatures):
        raise ValueError(f"The file content does not look like a {extension[1:].upper()} file.")
    if signatures is None:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Text and CSV evidence must be UTF-8.") from exc
    return {
        "evidence_name": name,
        "evidence_content_type": content_type,
        "evidence_size": len(data),
        "evidence_sha256": hashlib.sha256(data).hexdigest(),
        "evidence_data": data,
    }


class EvidenceFileField(forms.FileField):
    """Optional upload that fails validation with the same message the service would give."""

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("label", "Evidence file")
        kwargs.setdefault("help_text", "PDF, PNG, JPEG, TXT or CSV, up to 10 MiB.")
        super().__init__(**kwargs)

    def clean(self, data, initial=None):
        uploaded = super().clean(data, initial)
        if uploaded:
            try:
                fields = read_evidence(uploaded)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
            uploaded.seek(0)
            uploaded.evidence_fields = fields
        return uploaded


def evidence_fields(uploaded):
    """Fields for an upload, or {} when none. Read once and remembered on the upload, so a service
    that asks twice does not find the stream already consumed. A bad file is a ValidationError
    on `evidence`, the same message the form shows."""
    if not uploaded:
        return {}
    fields = getattr(uploaded, "evidence_fields", None)
    if fields is None:
        try:
            fields = read_evidence(uploaded)
        except ValueError as exc:
            raise ValidationError({"evidence": str(exc)}) from exc
        uploaded.evidence_fields = fields
    return fields


def download_response(obj):
    """Attachment response for any EvidenceMixin row loaded with `with_evidence()`."""
    response = HttpResponse(bytes(obj.evidence_data), content_type=obj.evidence_content_type)
    response["Content-Disposition"] = content_disposition_header(True, obj.evidence_name)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response
