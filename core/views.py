from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Count, Min, Q
from django.http import JsonResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.roles import can_authorize_release, effective_roles, has_role, require_staff
from analytical import selectors as analytical
from commercial.models import Quote, Submission
from commercial.permissions import QUOTE_ROLES
from commercial.selectors import quotes_with_latest
from laboratory import training as training_service
from laboratory.models import TrainingRecord
from laboratory.permissions import EQUIPMENT_ROLES
from laboratory.selectors import (
    equipment_due,
    equipment_with_due,
    lots_with_state,
    nonconformances_with_state,
)
from receipt.models import Sample
from receipt.scheduling import scheduled_tests
from receipt.subcontracts import filter_subcontracts_by_state, subcontracts_with_decision
from reporting.queries import staff_reports
from reporting.services import PREPARER_ROLES

from .models import AuditEvent, Lab


def _queue(label, rows, url, params, *, oldest=None, tone="info", note=""):
    """One dashboard queue. `rows` is the same queryset the linked list builds for `params`, so
    the count here is the count there. A single aggregate query gives count and oldest item."""
    totals = {"count": Count("pk", distinct=True)}
    if oldest:
        totals["oldest"] = Min(oldest)
    totals = rows.aggregate(**totals)
    return {
        "label": label,
        "count": totals["count"],
        "url": f"{reverse(url)}?{urlencode(params)}" if params else reverse(url),
        "oldest_at": totals.get("oldest"),
        "tone": tone,
        "note": note,
    }


def _work_queues(user):
    """Work waiting for this user, in work-sequence order. Each queue is shown to the roles that
    do that work (and may open its list); the lists themselves still authorize every request."""
    roles = effective_roles(user)
    now = timezone.now()
    today = timezone.localdate(now)
    queues = []
    receives = bool(roles & {"custodian", "project_manager"})

    if receives:
        queues.append(
            _queue(
                "Samples on hold at receipt",
                Sample.objects.filter(disposition="hold"),
                "receipt:sample-list",
                {"disposition": "hold"},
                oldest="received_at",
                tone="hold",
                note="Each needs a disposition decision.",
            )
        )
        queues.append(
            _queue(
                "Client submissions to accept",
                Submission.objects.filter(status=Submission.Status.SUBMITTED),
                "commercial:submission-list",
                {"status": Submission.Status.SUBMITTED},
                oldest="submitted_at",
            )
        )
        queues.append(
            _queue(
                "Tests past their delivery target",
                scheduled_tests()
                .exclude(stage="reported")
                .exclude(sample__disposition="reject")
                .filter(sample__order__due_at__lt=now),
                "receipt:schedule",
                {"deadline": "overdue"},
                oldest="sample__order__due_at",
                tone="danger",
            )
        )
    if roles & set(QUOTE_ROLES):
        queues.append(
            _queue(
                "Draft quotes",
                quotes_with_latest().filter(status=Quote.Status.DRAFT),
                "commercial:quote-list",
                {"status": Quote.Status.DRAFT},
                oldest="created_at",
            )
        )
    if roles & {"analyst"}:
        for label, status in (
            ("Tests not yet in a batch", "not_batched"),
            ("In a batch, awaiting entry", "awaiting"),
            ("Results returned for correction", "returned"),
        ):
            queues.append(
                _queue(
                    label,
                    analytical.worklist_queryset(status=status),
                    "analytical:batch-worklist",
                    {"status": status},
                    oldest="sample__received_at",
                    tone="warning" if status == "returned" else "info",
                )
            )
    if roles & {"analyst", "custodian", "project_manager"}:
        queue = _queue(
            "Holding time at risk or expired",
            analytical.worklist_queryset(holding="at_risk"),
            "analytical:batch-worklist",
            {"holding": "at_risk"},
            tone="danger",
        )
        expired = analytical.worklist_queryset(holding="expired").count() if queue["count"] else 0
        queue["note"] = f"{expired} already expired." if expired else ""
        queues.append(queue)
    if "reviewer" in roles:
        reviewable = analytical.review_queue_queryset(user).filter(reviewable__gt=0)
        queue = _queue(
            "Batches awaiting technical review",
            reviewable,
            "analytical:review-list",
            {},
            oldest="oldest_submitted",
            tone="progress",
        )
        own_only = analytical.review_queue_queryset(user).filter(reviewable__lte=0).count()
        if own_only:
            queue["note"] = (
                f"{own_only} more hold only results you entered; another reviewer must review them."
            )
        queues.append(queue)
        unreviewed = filter_subcontracts_by_state(subcontracts_with_decision(), "unreviewed")
        queue = _queue(
            "External source results awaiting review",
            unreviewed.exclude(latest_entered_by=user.pk),
            "receipt:subcontract-list",
            {"state": "unreviewed"},
            tone="progress",
        )
        own_external = unreviewed.filter(latest_entered_by=user.pk).count()
        if own_external:
            queue["note"] = (
                f"{own_external} more were entered by you; another reviewer must review them."
            )
        queues.append(queue)
    # reporting.services.can_prepare, from the roles already loaded above
    if roles & set(PREPARER_ROLES) or can_authorize_release(user):
        drafts = staff_reports(QueryDict("status=draft"))[2]
        queues.append(
            _queue(
                "Draft reports",
                drafts,
                "reporting:report-list",
                {"status": "draft"},
                oldest="created_at",
            )
        )
        changed = staff_reports(QueryDict("attention=changed"))[2]
        queues.append(
            _queue(
                "Issued reports with changed results",
                changed,
                "reporting:report-list",
                {"attention": "changed"},
                tone="warning",
                note="A change after issue needs an amended version.",
            )
        )
    if roles & set(EQUIPMENT_ROLES):
        queues.append(
            _queue(
                "Equipment calibration or maintenance overdue",
                equipment_due(equipment_with_due(), "overdue", today, 30),
                "laboratory:equipment-list",
                {"due": "overdue"},
                tone="danger",
            )
        )
        queues.append(
            _queue(
                "Standards and reagents expired but still on hand",
                lots_with_state(today=today).filter(
                    status="active", expires_on__lt=today, quantity_on_hand__gt=0
                ),
                "laboratory:lot-list",
                {"state": "expired"},
                tone="danger",
            )
        )
    if roles:
        queues.append(
            _queue(
                "Nonconformances assigned to me",
                nonconformances_with_state(today=today).filter(owner=user).exclude(status="closed"),
                "laboratory:nc-list",
                {"status": "not_closed", "owner": "me"},
                tone="warning",
            )
        )
        horizon = today + timedelta(days=30)
        mine = training_service.with_state(TrainingRecord.objects.filter(staff=user), today=today)
        queues.append(
            _queue(
                "My training records expired or expiring within 30 days",
                mine.filter(is_renewed=False).filter(
                    Q(state="expired") | Q(state="current", expires_on__lte=horizon)
                ),
                "laboratory:training-list",
                {"state": "attention", "staff": user.pk},
                tone="warning",
            )
        )
    return queues


@login_required
def dashboard(request):
    if request.user.client_id:
        return redirect("portal:home")
    require_staff(request.user)
    queues = _work_queues(request.user)
    return render(
        request,
        "core/dashboard.html",
        {
            "page_title": "Dashboard",
            "held_count": Sample.objects.filter(disposition="hold").count(),
            "queues": queues,
            "queues_clear": not any(queue["count"] for queue in queues),
            "recent_samples": Sample.objects.select_related("order__project__client")[:10],
        },
    )


def health(request):
    return JsonResponse({"status": "ok"})


def ready(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})


@login_required
def record_retention(request):
    require_staff(request.user)
    lab = Lab.objects.first()
    return render(
        request,
        "core/record_retention.html",
        {
            "page_title": "Record retention",
            "lab": lab,
            "statement": lab.record_retention_statement if lab else "",
            "can_manage_policy": request.user.is_staff
            and request.user.is_superuser
            and has_role(request.user, "admin"),
            "policy_events": AuditEvent.objects.filter(
                object_type="core.lab", object_id="1", action="lab.record_retention_changed"
            )
            .select_related("actor")
            .order_by("-occurred_at")[:20],
        },
    )
