import hashlib
import logging
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from accounts.roles import can_authorize_release, require_release_authority, require_staff
from core.grids import active_filters
from receipt.models import Project, Sample

from .content import assemble, describe_changes
from .forms import ProjectChoiceForm, ReleaseForm, ReportForm
from .models import MAX_SAMPLES, Report, ReportFile, ReportVersion
from .pdf import ReportRenderError, render_pdf
from .queries import (
    PER_PAGE,
    REPORT_COLUMNS,
    per_page,
    picker_samples,
    reports_by_sample,
    staff_reports,
)
from .services import (
    StaleReport,
    can_prepare,
    create_report,
    release_report,
    require_preparer,
    update_report,
)

log = logging.getLogger(__name__)

PREVIEW_EXPANDED_SAMPLES = 10  # longer previews collapse per-sample results


def pdf_response(version):
    """Stored bytes of an issued version, checked against the recorded hash."""
    data = bytes(ReportFile.objects.only("pdf").get(version=version).pdf)
    if hashlib.sha256(data).hexdigest() != version.pdf_sha256:
        log.error("Stored PDF hash mismatch for report version %s", version.pk)
        return HttpResponse(
            "This report file failed its integrity check. Contact the laboratory.",
            status=500,
            content_type="text/plain",
        )
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{version.filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def toolbar_fields(form, inline, more=()):
    """Bound fields for includes/grid_toolbar.html: everyday filters, then More filters, which
    opens when one of them holds a value (an invalid filter opens it in the include)."""
    shown = [form[name] for name in inline]
    extra = [form[name] for name in more]
    return {
        "inline": shown,
        "more": extra,
        "more_open": any(form.data.get(field.name) for field in extra),
    }


def _report(pk):
    return get_object_or_404(Report.objects.select_related("project__client", "created_by"), pk=pk)


def _versions(report):
    return list(report.versions.select_related("released_by", "amends").order_by("-number"))


@login_required
def report_list(request):
    require_staff(request.user)
    try:
        grid, form, reports = staff_reports(request.GET)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    page = grid.page(reports, REPORT_COLUMNS)
    latest = {
        v.report_id: v
        for v in ReportVersion.objects.filter(report__in=list(page))
        .select_related("released_by")
        .order_by("number")
    }
    for report in page:
        report.current = latest.get(report.pk)
    chips = active_filters(request, form) if form.is_valid() else []
    return render(
        request,
        "reporting/report_list.html",
        {
            "page_title": "Reports",
            "page_obj": page,
            "filter_form": form,
            "q": grid.query,
            "sort": grid.sort,
            "per_page": grid.per_page,
            "active_filters": chips,
            "filtered": bool(chips) or not form.is_valid(),
            "can_prepare": can_prepare(request.user),
            **toolbar_fields(
                form,
                ("status", "client", "attention"),
                ("project", "released_from", "released_to"),
            ),
        },
    )


def _scope_form(request, project, report=None):
    """Report scope form with the sample picker. Picker navigation (search, filter, page) posts
    the whole form and re-renders without saving or validating, so typed fields and the
    selection survive; selected samples outside the visible page travel as hidden inputs."""
    posted = request.method == "POST"
    data = request.POST if posted else request.GET
    navigating = posted and ("picker" in data or "ppage" in data)
    project_ids = Sample.objects.filter(order__project=project)
    if posted:
        selected = set(
            str(pk)
            for pk in project_ids.filter(pk__in=_uuids(data.getlist("samples"))).values_list(
                "pk", flat=True
            )
        )
    elif report is not None:
        selected = {str(pk) for pk in report.samples.values_list("pk", flat=True)}
    else:
        selected = set()
    initial = (
        {"title": report.title, "narrative": report.narrative}
        if report
        else {"title": f"{project.name} analytical report"}
    )
    if posted and navigating:
        initial = {"title": data.get("title", ""), "narrative": data.get("narrative", "")}
        form = ReportForm(project=project, initial=initial)
    elif posted:
        form = ReportForm(data, project=project)
    else:
        form = ReportForm(project=project, initial=initial)
    rows, show = picker_samples(project, data, selected)
    page = Paginator(rows, per_page(data.get("pper"))).get_page(
        1 if "picker" in data else data.get("ppage")
    )
    visible = {str(s.pk) for s in page}
    elsewhere = reports_by_sample([s.pk for s in page])
    for sample in page:
        sample.other_reports = [r for r in elsewhere.get(sample.pk, []) if r != report]
    return (
        form,
        navigating,
        {
            "picker_page": page,
            "picker_show": show,
            "picker_q": (data.get("pq") or "").strip(),
            "picker_per_page": per_page(data.get("pper")),
            "per_page_options": PER_PAGE,
            "selected_ids": selected,
            "hidden_selected": sorted(selected - visible),
            "selected_count": len(selected),
            "max_samples": MAX_SAMPLES,
            "project_sample_count": project_ids.count(),
        },
    )


def _uuids(values):
    out = []
    for value in values:
        try:
            out.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return out


@login_required
def report_create(request):
    require_preparer(request.user)
    chooser = ProjectChoiceForm(request.GET or None)
    project = chooser.cleaned_data["project"] if chooser.is_valid() else None
    context = {}
    form = None
    if project:
        form, navigating, context = _scope_form(request, project)
        if request.method == "POST" and not navigating and form.is_valid():
            try:
                report = create_report(actor=request.user, project=project, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(
                    request, f"Report {report.number} prepared. Preview it to check readiness."
                )
                return redirect("reporting:report-detail", pk=report.pk)
    return render(
        request,
        "reporting/report_form.html",
        {
            "page_title": "Prepare report",
            "chooser": chooser,
            "project": project,
            "form": form,
            "has_projects": Project.objects.exists(),
            **context,
        },
    )


@login_required
def report_edit(request, pk):
    require_preparer(request.user)
    report = _report(pk)
    form, navigating, context = _scope_form(request, report.project, report)
    if request.method == "POST" and not navigating and form.is_valid():
        try:
            expected = int(request.POST.get("expected_revision", ""))
        except ValueError:
            expected = -1
        try:
            update_report(
                actor=request.user,
                report_id=report.pk,
                expected_revision=expected,
                **form.cleaned_data,
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Report scope saved. Issued versions are unchanged.")
            return redirect("reporting:report-detail", pk=report.pk)
    return render(
        request,
        "reporting/report_form.html",
        {
            "page_title": f"Edit {report.number}",
            "report": report,
            "project": report.project,
            "form": form,
            "expected_revision": request.POST.get("expected_revision", report.revision),
            **context,
        },
    )


@login_required
def report_detail(request, pk):
    require_staff(request.user)
    report = _report(pk)
    assembly = assemble(report)
    versions = _versions(report)
    current = versions[0] if versions else None
    return render(
        request,
        "reporting/report_detail.html",
        {
            "page_title": report.number,
            "report": report,
            "assembly": assembly,
            "versions": versions,
            "current": current,
            "differs": current is not None and current.content_sha256 != assembly.fingerprint,
            "can_prepare": can_prepare(request.user),
            "can_release": can_authorize_release(request.user),
        },
    )


def _preview(request, report, *, posted=None, errors=(), status=200):
    """Preview page. A posted release form is shown again with its field errors only when
    the content is unchanged; otherwise a fresh form must be confirmed for the new content."""
    report.refresh_from_db()
    can_release = can_authorize_release(request.user)
    assembly = assemble(report, actor=request.user if can_release else None)
    amendment = report.latest_version > 0
    changes, current = [], None
    if amendment:
        current = report.versions.select_related("file").get(number=report.latest_version)
        changes = describe_changes(current.file.snapshot["content"], assembly.content)
    if posted is not None and not errors and posted.data.get("fingerprint") == assembly.fingerprint:
        form = posted
    else:
        reason = posted.data.get("reason", "") if posted is not None else ""
        form = ReleaseForm(
            amendment=amendment, initial={"fingerprint": assembly.fingerprint, "reason": reason}
        )
    toc = []
    if assembly.blockers:
        toc.append(("blockers-heading", f"Blocking issues ({len(assembly.blockers)})"))
    if amendment:
        toc.append(("changes-heading", f"Changes from v{current.number}"))
    if not assembly.blockers and not amendment:
        toc.append(("ready-heading", "Ready to release"))
    toc.append(("release", "Release"))
    if assembly.content["samples"]:
        toc.append(("sample-1", f"Samples ({len(assembly.content['samples'])})"))
    toc.append(("pdf-heading", "PDF preview"))
    return render(
        request,
        "reporting/report_preview.html",
        {
            "page_title": f"Preview {report.number}",
            "report": report,
            "assembly": assembly,
            "c": assembly.content,
            "form": form,
            "release_errors": errors,
            "amendment": amendment,
            "changes": changes,
            "current": current,
            "next_version": report.latest_version + 1,
            "report_blockers": assembly.report_blockers,
            "expand_samples": len(assembly.content["samples"]) <= PREVIEW_EXPANDED_SAMPLES,
            "can_release": can_release,
            "toc": toc,
        },
        status=status,
    )


@login_required
def report_preview(request, pk):
    require_staff(request.user)
    return _preview(request, _report(pk))


@login_required
@xframe_options_sameorigin
def report_preview_pdf(request, pk):
    require_staff(request.user)
    report = _report(pk)
    assembly = assemble(report)
    expected = request.GET.get("fp")
    if expected and expected != assembly.fingerprint:
        return HttpResponse(
            "The report content changed. Reload the preview page.",
            status=409,
            content_type="text/plain",
        )
    snapshot = {
        "content": assembly.content,
        "content_sha256": assembly.fingerprint,
        "release": None,
    }
    try:
        pdf = render_pdf(snapshot, preview=True)
    except ReportRenderError as exc:
        log.exception("Preview rendering failed for report %s", report.pk)
        return HttpResponse(str(exc), status=503, content_type="text/plain")
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{report.number}-preview.pdf"'
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
def report_release(request, pk):
    require_release_authority(request.user)
    report = _report(pk)
    form = ReleaseForm(request.POST, amendment=report.latest_version > 0)
    if not form.is_valid():
        return _preview(request, report, posted=form, status=422)
    try:
        version = release_report(
            actor=request.user,
            report_id=report.pk,
            expected_fingerprint=form.cleaned_data["fingerprint"],
            reason=form.cleaned_data.get("reason", ""),
        )
    except StaleReport as exc:
        return _preview(request, report, posted=form, errors=exc.messages, status=409)
    except ValidationError as exc:
        return _preview(request, report, posted=form, errors=exc.messages, status=422)
    except ReportRenderError as exc:
        log.exception("Release rendering failed for report %s", report.pk)
        return _preview(request, report, posted=form, errors=[str(exc)], status=503)
    messages.success(
        request, f"{version.label} released. Its PDF is stored and available to the client."
    )
    return redirect("reporting:report-detail", pk=report.pk)


@login_required
def version_pdf(request, pk, number):
    require_staff(request.user)
    version = get_object_or_404(
        ReportVersion.objects.select_related("report"), report_id=pk, number=number
    )
    return pdf_response(version)
