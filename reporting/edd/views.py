import hashlib
import logging
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_safe

from accounts.roles import RECEIPT_ROLES, has_role, require_staff
from analytical.models import Qualifier
from catalog.models import TestDefinition
from receipt.corrections import correction_blocker
from receipt.models import Project

from ..content import assemble
from ..models import (
    EddExport,
    EddVersionContext,
    NysdecProjectSettings,
    NysdecQualifierMapping,
    NysdecTestMapping,
    Report,
    ReportVersion,
)
from ..portal_views import client_required, client_versions
from . import fixes, services
from .forms import NysdecProjectForm, NysdecQualifierForm, NysdecTestForm, VersionContextForm
from .nysdec import LAB_QC_TYPES, NOTICE, SECTION_ORDER
from .nysdec_format import FORMAT_NAME, FORMAT_VERSION, REFERENCE_VALUES, SECTIONS
from .sources import revision_facts

log = logging.getLogger(__name__)
PREVIEW_ROWS = 20


def export_response(export):
    data = bytes(EddExport.objects.only("data", "sha256").get(pk=export.pk).data)
    if hashlib.sha256(data).hexdigest() != export.sha256:
        log.error("Stored EDD hash mismatch for export %s", export.pk)
        return HttpResponse(
            "This export failed its integrity check. Contact the laboratory.",
            status=500,
            content_type="text/plain",
        )
    response = HttpResponse(data, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{export.filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _version(pk, number):
    return get_object_or_404(
        ReportVersion.objects.select_related("report__project__client", "released_by"),
        report_id=pk,
        number=number,
    )


@login_required
def edd_preview(request, pk, number):
    """Validation preview for one released version; POST generates an immutable export."""
    require_staff(request.user)
    version = _version(pk, number)
    errors = []
    if request.method == "POST":
        try:
            export = services.generate_export(actor=request.user, version_id=version.pk)
        except ValidationError as exc:
            errors = exc.messages
        else:
            messages.success(
                request,
                f"Export {export.pk} generated ({export.size} bytes). "
                "It is exploratory until EQuIS EDP accepts it.",
            )
            return redirect("reporting:edd-preview", pk=pk, number=number)
    result, _ = services.preview(version)
    place = fixes.Place(
        project_id=version.report.project_id,
        here=reverse("reporting:edd-preview", args=[version.report_id, version.number]),
        context_url=reverse("reporting:edd-context", args=[version.report_id, version.number]),
    )
    groups, fix_summary = fixes.issue_groups(result.errors, place)
    tables = []
    for section in SECTION_ORDER:
        names = [
            c.name
            for c in SECTIONS[section]
            if any(row[c.name] for row in result.tables[section]) or c.required
        ]
        rows = [[row[n] for n in names] for row in result.tables[section][:PREVIEW_ROWS]]
        tables.append((section, names, rows, len(result.tables[section])))
    return render(
        request,
        "reporting/edd/preview.html",
        {
            "page_title": f"NYSDEC EDD · {version.report.number} v{version.number}",
            "version": version,
            "report": version.report,
            "result": result,
            "issue_groups": groups,
            "fix_summary": fix_summary,
            "post_errors": errors,
            "tables": tables,
            "preview_rows": PREVIEW_ROWS,
            "notice": NOTICE,
            "format_name": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "reference_values": REFERENCE_VALUES,
            "exports": version.edd_exports.select_related("created_by"),
            "can_generate": services.can_configure(request.user),
        },
        status=422 if errors else 200,
    )


@login_required
def edd_download(request, export_pk):
    require_staff(request.user)
    return export_response(
        get_object_or_404(EddExport.objects.select_related("version__report"), pk=export_pk)
    )


@client_required
def portal_edd_download(request, pk, export_pk):
    export = get_object_or_404(
        EddExport.objects.select_related("version__report").filter(
            version__in=client_versions(request.user)
        ),
        pk=export_pk,
        version__report_id=pk,
    )
    return export_response(export)


def _next(request, default):
    target = request.GET.get("next") or request.POST.get("next")
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}):
        return target
    return default


def _edit(request, *, form, title, save, back, context):
    if request.method == "POST" and form.is_valid():
        try:
            save(form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f"{title} saved. Existing exports are unchanged.")
            return redirect(back)
    return render(
        request,
        "reporting/edd/mapping_form.html",
        {
            "page_title": title,
            "form": form,
            "back": back,
            **context,
        },
    )


@login_required
def nysdec_project(request, project_pk):
    services.require_configure(request.user)
    project = get_object_or_404(Project.objects.select_related("client"), pk=project_pk)
    instance = NysdecProjectSettings.objects.filter(project=project).first()
    form = NysdecProjectForm(request.POST or None, instance=instance)
    return _edit(
        request,
        form=form,
        title=f"NYSDEC codes · {project.name}",
        save=lambda data: services.save_project_settings(
            actor=request.user, project_id=project.pk, **data
        ),
        back=_next(request, reverse("reporting:report-list")),
        context={"subject": f"{project.client} / {project.name}"},
    )


@login_required
def nysdec_test(request, definition_pk):
    services.require_configure(request.user)
    definition = get_object_or_404(
        TestDefinition.objects.select_related("method", "analyte"), pk=definition_pk
    )
    instance = NysdecTestMapping.objects.filter(definition=definition).first()
    form = NysdecTestForm(request.POST or None, instance=instance)
    return _edit(
        request,
        form=form,
        title=f"NYSDEC mapping · {definition}",
        save=lambda data: services.save_test_mapping(
            actor=request.user, definition_id=definition.pk, **data
        ),
        back=_next(request, reverse("reporting:report-list")),
        context={
            "subject": f"{definition.analyte} (CAS {definition.analyte.cas_number or 'not set'})"
            f" · {definition.method} · unit {definition.unit}"
        },
    )


@login_required
def nysdec_qualifier(request, qualifier_pk):
    services.require_configure(request.user)
    qualifier = get_object_or_404(Qualifier, pk=qualifier_pk)
    instance = NysdecQualifierMapping.objects.filter(qualifier=qualifier).first()
    form = NysdecQualifierForm(request.POST or None, instance=instance)
    return _edit(
        request,
        form=form,
        title=f"NYSDEC qualifier · {qualifier.code}",
        save=lambda data: services.save_qualifier_mapping(
            actor=request.user, qualifier_id=qualifier.pk, **data
        ),
        back=_next(request, reverse("reporting:report-list")),
        context={"subject": f"Lab qualifier {qualifier.code}: {qualifier.description}"},
    )


@login_required
def edd_context(request, pk, number):
    """Export-time depth/location and retest type for one released version."""
    services.require_configure(request.user)
    version = _version(pk, number)
    snapshot = version.file.snapshot
    facts = revision_facts(snapshot)
    samples = [s for s in snapshot["content"]["samples"] if s["results"]]
    corrected = [
        {**r, "lab_id": s["lab_id"]}
        for s in samples
        for r in s["results"]
        if r["revision"] > 1
        and r["revision_id"] in facts
        and (
            datetime.fromisoformat(r["analysis_started_at"])
            != facts[r["revision_id"]].first_analysis_started_at
            or (r.get("preparation") or {}).get("aliquot_id")
            != facts[r["revision_id"]].first_prepared_aliquot_id
        )
    ]
    saved = EddVersionContext.objects.filter(version=version).first()
    form = VersionContextForm(
        request.POST or None,
        samples=samples,
        results=corrected,
        initial_samples=saved.samples if saved else {},
        initial_results=saved.results if saved else {},
        qc_rows=_lab_qc_rows(snapshot),
        initial_qc=saved.qc_samples if saved else {},
    )
    preview_url = reverse("reporting:edd-preview", args=[pk, number])
    if request.method == "POST" and form.is_valid():
        sample_values, result_values, qc_values = form.values()
        services.save_version_context(
            actor=request.user,
            version_id=version.pk,
            samples=sample_values,
            results=result_values,
            qc_samples=qc_values,
        )
        messages.success(
            request,
            "Export context saved for this version. Existing exports are "
            "unchanged; the released report is unchanged.",
        )
        return redirect(preview_url)
    return render(
        request,
        "reporting/edd/context_form.html",
        {
            "page_title": f"Export context · {version.report.number} v{version.number}",
            "version": version,
            "report": version.report,
            "form": form,
            "back": preview_url,
        },
    )


def _lab_qc_rows(snapshot):
    """Exported lab QC samples (method blank, LCS) of the frozen evidence, with the batch's frozen
    preparation time as a hint only."""
    content = snapshot["content"]
    prep = {
        r["batch"]: r.get("prepared_at")
        for s in content["samples"]
        for r in s["results"]
        if r.get("prepared_at")
    }
    rows = {}
    for batch in content["batches"]:
        for analyte, evidence in batch["qc"].items():
            for m in evidence.get("measurements", []):
                if m.get("withheld") or m["qc_type"] not in LAB_QC_TYPES:
                    continue
                code = f"{batch['number']}-{m['qc_sample']}"
                row = rows.setdefault(
                    code,
                    {
                        "code": code,
                        "batch": batch["number"],
                        "qc_type": m["qc_type"],
                        "analytes": [],
                        "prepared_at": prep.get(batch["number"]),
                    },
                )
                row["analytes"].append(analyte)
    return list(rows.values())


def _identity_blockers(assembly, result):
    """Reason the receipt identity correction is refused, for samples with an identity finding."""
    wanted = {i.lab_id for i in result.errors if i.field in fixes.IDENTITY_FIELDS}
    return {
        st.sample.lab_id: correction_blocker(st.sample, "identity")
        for st in assembly.samples
        if st.sample.lab_id in wanted
    }


@login_required
@require_safe
def edd_readiness(request, pk):
    """Optional early NYSDEC check of a draft report. Read-only: it assembles the same content a
    release would freeze, runs the same versioned-format rules as the released-version preview
    and creates no version, export, setting or audit record."""
    require_staff(request.user)
    report = get_object_or_404(Report.objects.select_related("project__client"), pk=pk)
    assembly = assemble(report)
    result = services.draft_preview(report, assembly.content)
    place = fixes.Place(
        project_id=report.project_id,
        here=reverse("reporting:edd-readiness", args=[report.pk]),
        draft=True,
        samples={st.sample.lab_id: st.sample.pk for st in assembly.samples},
        blocked=_identity_blockers(assembly, result),
        can_configure=services.can_configure(request.user),
        can_receive=has_role(request.user, *RECEIPT_ROLES),
    )
    groups, _ = fixes.issue_groups(result.errors, place)
    findings = [g for g in groups if g["kind"] not in ("context", "unsupported")]
    later = [g for g in groups if g["kind"] == "context"]
    unsupported = [g for g in groups if g["kind"] == "unsupported"]
    finding_count = sum(len(g["records"]) for g in findings)
    rows = [r for s in assembly.content["samples"] for r in s["results"]]
    return render(
        request,
        "reporting/edd/readiness.html",
        {
            "page_title": f"NYSDEC data check · {report.number}",
            "report": report,
            "assembly": assembly,
            "result": result,
            "findings": findings,
            "finding_count": finding_count,
            "finding_label": f"{finding_count} finding{'s' * (finding_count != 1)} in "
            f"{len(findings)} cause{'s' * (len(findings) != 1)}",
            "fix_summary": fixes.summarize(findings),
            "later": later,
            "later_count": sum(len(g["records"]) for g in later),
            "unsupported": unsupported,
            "unsupported_count": sum(len(g["records"]) for g in unsupported),
            "checked_results": sum(1 for r in rows if r.get("source") != "external"),
            "incomplete_tests": sum(1 for st in assembly.samples for t in st.tests if t.problems),
            "incomplete_samples": sum(1 for st in assembly.samples if st.blockers),
            "can_configure": place.can_configure,
            "format_name": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "reference_values": REFERENCE_VALUES,
            "tables": [(n, len(result.tables[n])) for n in SECTION_ORDER],
        },
    )
