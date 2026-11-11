"""Recipient mapping edits and NYSDEC EDD generation. Each mutation authorizes the actor and
writes its audit event in the same transaction. Exports read only the frozen release snapshot,
immutable result revisions and the mapping values, which are frozen into the export record."""

import hashlib

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from accounts.roles import has_role
from core.audit import record_event
from receipt.models import Project

from ..models import (
    MAX_PDF_BYTES,
    EddExport,
    EddVersionContext,
    NysdecProjectSettings,
    NysdecQualifierMapping,
    NysdecTestMapping,
    ReportVersion,
)
from ..services import can_prepare
from . import nysdec
from .nysdec_format import ENUMERATIONS, FORMAT_NAME, FORMAT_VERSION, REFERENCE_VALUES
from .sources import qc_facts, revision_facts

TEST_FIELDS = (
    "lab_anl_method_name",
    "preparation_method_code",
    "preparation_method_version",
    "prep_method",
    "fraction",
    "preservative",
    "basis",
    "chemical_name",
    "unit_code",
)
PROJECT_FIELDS = (
    "data_provider",
    "sampling_company_code",
    "lab_name_code",
    "task_code",
    "water_matrix_code",
    "wastewater_matrix_code",
    "soil_matrix_code",
    "water_qc_matrix_code",
    "wastewater_qc_matrix_code",
    "soil_qc_matrix_code",
)


def can_configure(user):
    """Recipient codes are laboratory configuration: preparers, release authority or admin."""
    return can_prepare(user) or has_role(user, "admin")


def require_configure(user):
    if not can_configure(user):
        raise PermissionDenied(
            "Editing EDD codes needs the project manager, approver or admin role."
        )


def mapping_for(version):
    """nysdec.Mapping from the current recipient settings for the version's project."""
    context = EddVersionContext.objects.filter(version=version).first()
    return mapping_from(version.report.project_id, version.file.snapshot, context)


def mapping_from(project_id, snapshot, context=None):
    """Mapping for any snapshot-shaped content. `context` is a version's saved export context;
    None (a draft) means none is recorded yet."""
    settings = NysdecProjectSettings.objects.filter(project_id=project_id).first()
    facts = revision_facts(snapshot)
    definition_ids = {f.definition_id for f in facts.values()}
    tests = {
        m.definition_id: {k: getattr(m, k) for k in TEST_FIELDS}
        for m in NysdecTestMapping.objects.filter(definition_id__in=definition_ids)
    }
    qualifiers = dict(
        NysdecQualifierMapping.objects.values_list("qualifier__code", "interpreted_qualifier")
    )
    project = {k: getattr(settings, k) for k in PROJECT_FIELDS} if settings else {}
    matrix_codes = {
        label: project.get(name, "") for label, name in NysdecProjectSettings.MATRIX_FIELDS.items()
    }
    mapping = nysdec.Mapping(
        data_provider=project.get("data_provider", ""),
        sampling_company_code=project.get("sampling_company_code", ""),
        lab_name_code=project.get("lab_name_code", ""),
        task_code=project.get("task_code", ""),
        matrix_codes=matrix_codes,
        tests=tests,
        qualifiers=qualifiers,
        samples=context.samples if context else {},
        results=context.results if context else {},
        qc_matrix_codes={
            "water": project.get("water_qc_matrix_code", ""),
            "wastewater": project.get("wastewater_qc_matrix_code", ""),
            "soil": project.get("soil_qc_matrix_code", ""),
        },
        qc_samples=context.qc_samples if context else {},
    )
    return mapping, snapshot, facts


def preview(version):
    mapping, snapshot, facts = mapping_for(version)
    return nysdec.build(snapshot, mapping, facts, qc_facts(snapshot)), mapping


def draft_preview(report, content):
    """Read-only NYSDEC check of a draft report's assembled content with the current recipient
    settings and no version context. Writes nothing and creates no export."""
    mapping, snapshot, facts = mapping_from(report.project_id, {"content": content})
    return nysdec.build(snapshot, mapping, facts, qc_facts(snapshot))


def _frozen(mapping):
    return {
        "data_provider": mapping.data_provider,
        "sampling_company_code": mapping.sampling_company_code,
        "lab_name_code": mapping.lab_name_code,
        "task_code": mapping.task_code,
        "matrix_codes": mapping.matrix_codes,
        "tests": {str(k): v for k, v in sorted(mapping.tests.items())},
        "qualifiers": dict(sorted(mapping.qualifiers.items())),
        "qc_matrix_codes": mapping.qc_matrix_codes,
        "version_context": {
            "samples": mapping.samples,
            "results": mapping.results,
            "qc_samples": mapping.qc_samples,
        },
    }


def generate_export(*, actor, version_id):
    """Create an immutable export for a released version, or raise ValidationError listing every
    blocking issue. Bounded synchronous work: at most MAX_RESULTS rows from one snapshot."""
    require_configure(actor)
    with transaction.atomic():
        version = ReportVersion.objects.select_related("report", "file").get(pk=version_id)
        result, mapping = preview(version)
        if not result.ok:
            raise ValidationError([f"{i.record}: {i.message}" for i in result.errors])
        data = nysdec.write_zip(result.tables)
        if len(data) > MAX_PDF_BYTES:
            raise ValidationError("The export is larger than the 10 MiB file limit.")
        export = EddExport.objects.create(
            version=version,
            format_name=FORMAT_NAME,
            format_version=FORMAT_VERSION,
            reference_values=REFERENCE_VALUES,
            mapping=_frozen(mapping),
            row_counts=result.counts(),
            manifest=result.manifest,
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            created_by=actor,
        )
        record_event(
            actor=actor,
            action="report.edd_exported",
            obj=export,
            after={
                "report": version.report.number,
                "version": version.number,
                "format": f"{FORMAT_NAME} {FORMAT_VERSION}",
                "reference_values": REFERENCE_VALUES,
                "sha256": export.sha256,
                "row_counts": export.row_counts,
                "content_sha256": version.content_sha256,
            },
        )
    return export


def _save(actor, model, lookup, fields, data, action):
    with transaction.atomic():
        instance = model.objects.select_for_update().filter(**lookup).first()
        before = {k: getattr(instance, k) for k in fields} if instance else {}
        if instance is None:
            instance = model(**lookup)
        for k in fields:
            setattr(instance, k, (data.get(k) or "").strip())
        instance.updated_by = actor
        instance.full_clean()
        instance.save()
        record_event(
            actor=actor,
            action=action,
            obj=instance,
            before=before,
            after={k: getattr(instance, k) for k in fields},
        )
    return instance


def save_project_settings(*, actor, project_id, **data):
    require_configure(actor)
    Project.objects.get(pk=project_id)
    task = (data.get("task_code") or "").strip()
    if task and task not in ENUMERATIONS["task_code"]:
        raise ValidationError(
            {"task_code": "Choose one of " + ", ".join(ENUMERATIONS["task_code"]) + "."}
        )
    return _save(
        actor,
        NysdecProjectSettings,
        {"project_id": project_id},
        PROJECT_FIELDS,
        data,
        "edd.nysdec_project_saved",
    )


def save_test_mapping(*, actor, definition_id, **data):
    require_configure(actor)
    return _save(
        actor,
        NysdecTestMapping,
        {"definition_id": definition_id},
        TEST_FIELDS,
        data,
        "edd.nysdec_test_saved",
    )


def save_qualifier_mapping(*, actor, qualifier_id, interpreted_qualifier):
    require_configure(actor)
    return _save(
        actor,
        NysdecQualifierMapping,
        {"qualifier_id": qualifier_id},
        ("interpreted_qualifier",),
        {"interpreted_qualifier": interpreted_qualifier},
        "edd.nysdec_qualifier_saved",
    )


def save_version_context(*, actor, version_id, samples, results, qc_samples=None):
    """Export-time facts for one released version (never written into its snapshot)."""
    require_configure(actor)
    with transaction.atomic():
        version = ReportVersion.objects.get(pk=version_id)
        context = EddVersionContext.objects.select_for_update().filter(version=version).first()
        before = (
            {"samples": context.samples, "results": context.results, "qc": context.qc_samples}
            if context
            else {}
        )
        if context is None:
            context = EddVersionContext(version=version)
        context.samples = {k: v for k, v in samples.items() if any(v.values())}
        context.results = {k: v for k, v in results.items() if v.get("test_type")}
        context.qc_samples = {k: v for k, v in (qc_samples or {}).items() if any(v.values())}
        context.updated_by = actor
        context.save()
        record_event(
            actor=actor,
            action="edd.version_context_saved",
            obj=context,
            before=before,
            after={
                "report": version.report.number,
                "version": version.number,
                "samples": context.samples,
                "results": context.results,
                "qc_samples": context.qc_samples,
            },
        )
    return context
