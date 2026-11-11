"""Strict parsing of supplied result values and timestamps. Nothing is coerced or rounded."""

import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError

from .models import DECIMAL_PLACES, ResultKind

MAX_TEXT_LENGTH = 60
INTEGER_DIGITS = 24 - DECIMAL_PLACES
_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
_QUANTUM = Decimal(1).scaleb(-DECIMAL_PLACES)


def parse_decimal(text, label="Value"):
    """Parse a plain decimal number exactly. Rejects anything that would need rounding."""
    text = (text or "").strip()
    if not text:
        raise ValidationError(f"{label} is required.")
    if not _NUMBER.fullmatch(text):
        raise ValidationError(
            f"{label} '{text}' is not a plain number. Use digits with an optional decimal "
            "point; no thousands separators, units or comparison symbols."
        )
    value = Decimal(text)
    if value.adjusted() >= INTEGER_DIGITS:
        raise ValidationError(f"{label} '{text}' is too large to store.")
    if value != value.quantize(_QUANTUM):
        raise ValidationError(
            f"{label} '{text}' has more than {DECIMAL_PLACES} decimal places and would be "
            "rounded. Enter a value that can be stored exactly."
        )
    return value


def parse_result_text(text):
    """Return (kind, value) for supplied result text: a number, or ND for non-detect."""
    if text is None or len(text) > MAX_TEXT_LENGTH:
        raise ValidationError(f"Result text must be 1 to {MAX_TEXT_LENGTH} characters.")
    stripped = text.strip()
    if stripped.upper() == "ND":
        return ResultKind.NON_DETECT, None
    if stripped[:1] in "<>":
        raise ValidationError(
            f"'{stripped}' is a comparison, not a result. Enter ND for a non-detect and "
            "give the reporting limit separately."
        )
    return ResultKind.DETECTED, parse_decimal(stripped, "Result")


def parse_timestamp(text, lab_timezone, label="Date and time"):
    """Parse an ISO 8601 timestamp. A value without an offset is read in the given zone (callers
    pass settings.TIME_ZONE); ambiguous or non-existent local times (daylight saving changes)
    are rejected."""
    text = (text or "").strip()
    if not text:
        raise ValidationError(f"{label} is required.")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"{label} '{text}' is not ISO 8601, for example 2026-11-08T14:30 or "
            "2026-11-08T14:30-05:00."
        ) from None
    if parsed.tzinfo is not None:
        return parsed
    zone = ZoneInfo(lab_timezone)
    first, second = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValidationError(
            f"{label} '{text}' is ambiguous or does not exist in {lab_timezone} (daylight "
            "saving change). Include a UTC offset."
        )
    return first
