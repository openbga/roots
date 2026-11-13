from datetime import datetime

from django import template

from analytical.models import QCType
from reporting.pdf import logo_url as snapshot_logo_url

register = template.Library()


@register.filter
def iso_time(value):
    """Format a snapshot ISO 8601 timestamp as 'YYYY-MM-DD HH:MM (UTC-05:00)'."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except TypeError, ValueError:
        return value
    offset = parsed.strftime("%z")
    if offset:
        offset = f" (UTC{offset[:3]}:{offset[3:]})"
    return parsed.strftime("%Y-%m-%d %H:%M") + offset


@register.filter
def iso_minute(value):
    """The date and time of a snapshot ISO 8601 timestamp, 'YYYY-MM-DD HH:MM' (no offset)."""
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except TypeError, ValueError:
        return value


@register.filter
def iso_offset(value):
    """The UTC offset of a snapshot ISO 8601 timestamp, 'UTC-05:00', or '' when it has none."""
    try:
        offset = datetime.fromisoformat(value).strftime("%z")
    except TypeError, ValueError:
        return ""
    return f"UTC{offset[:3]}:{offset[3:]}" if offset else ""


@register.filter
def iso_date(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except TypeError, ValueError:
        return value


@register.filter
def logo_url(logo):
    """Internal URL of a snapshot's laboratory logo, served only by the PDF renderer."""
    return snapshot_logo_url(logo)


@register.filter
def words(value):
    """Stored choice key as words: 'method_blank' -> 'method blank'."""
    return str(value or "").replace("_", " ")


@register.filter
def qc_type_label(value):
    """QC type key as its display label: 'lcs' -> 'Laboratory control sample'."""
    try:
        return QCType(value).label
    except ValueError:
        return words(value)


BASIS_LABELS = {"wet": "Wet weight", "dry": "Dry weight"}


@register.filter
def basis_label(row, matrix):
    """Reporting basis of an internal soil result as frozen in its snapshot. Rows without the key
    (issued before it existed, or legacy results) are "Basis not recorded" for soil; subcontracted
    rows and non-soil matrices show nothing. Never inferred from the catalog."""
    if row.get("source") == "external":
        return ""
    basis = row.get("result_basis")
    if basis in BASIS_LABELS:
        return BASIS_LABELS[basis]
    if basis == "na":
        return ""
    return "Basis not recorded" if str(matrix).lower() == "soil" else ""


@register.filter
def solids_text(row):
    """Supplied percent solids with its reference, exactly as frozen."""
    value = row.get("percent_solids")
    if value is None:
        return ""
    reference = row.get("solids_reference")
    return f"{value}% solids" + (f" ({reference})" if reference else "")


@register.filter
def amendment_change(change):
    """One stored amendment change as {"text", and for a replaced value "subject", "old", "new"}.
    Versions issued before structured changes stored plain sentences; they print as they are."""
    return {"text": change} if isinstance(change, str) else change
