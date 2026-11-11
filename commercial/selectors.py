"""Annotated querysets shared by staff and portal lists."""

from django.db.models import BooleanField, Case, Count, F, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce, NullIf
from django.utils import timezone

from .models import MISSING_TITLE, PriceList, Quote, QuoteVersion, Submission


def _frozen(versions, field, live, missing, issued_only):
    frozen = Subquery(versions.values(field)[:1])
    if issued_only:
        # An empty frozen project is a real value; only a missing title is reported as missing.
        return Coalesce(NullIf(frozen, Value("")), Value(missing)) if missing else frozen
    return Coalesce(NullIf(frozen, Value("")), live)


def quotes_with_latest(queryset=None, *, issued_only=False):
    """Latest (or latest issued) version's number, total, validity and issue time on each quote."""
    versions = QuoteVersion.objects.filter(quote=OuterRef("pk"))
    if issued_only:
        versions = versions.filter(issued_at__isnull=False)
    versions = versions.order_by("-number")
    queryset = Quote.objects.all() if queryset is None else queryset
    return queryset.select_related("client", "project").annotate(
        latest_number=Subquery(versions.values("number")[:1]),
        latest_total=Subquery(versions.values("total")[:1]),
        latest_valid_until=Subquery(versions.values("valid_until")[:1]),
        latest_issued_at=Subquery(versions.values("issued_at")[:1]),
        # What the client sees comes from the issued version, never from the live (draftable)
        # quote. Staff lists, which show live values, keep the live fallback.
        latest_title=_frozen(versions, "title", F("title"), MISSING_TITLE, issued_only),
        latest_project_name=_frozen(versions, "project_name", F("project__name"), "", issued_only),
        is_expired=Case(
            When(
                status=Quote.Status.ISSUED, latest_valid_until__lt=timezone.localdate(), then=True
            ),
            default=Value(False),
            output_field=BooleanField(),
        ),
    )


def price_lists_with_counts():
    return PriceList.objects.annotate(item_count=Count("items"))


def submissions_with_counts(queryset=None):
    queryset = Submission.objects.all() if queryset is None else queryset
    return queryset.select_related("client", "project").annotate(
        sample_count=Count("samples", distinct=True),
        received_count=Count("samples", filter=Q(samples__sample__isnull=False), distinct=True),
        pending_count=F("sample_count") - F("received_count"),
    )


def history(obj):
    from core.models import AuditEvent

    return AuditEvent.objects.filter(
        object_type=obj._meta.label_lower, object_id=str(obj.pk)
    ).select_related("actor")
