"""Catalog changes: authorization, edit-lock rules, validation and audit in one transaction.

Rules, kept deliberately simple:
- Only the admin role changes the catalog. Every create and edit is audited with before and
  after values; changing an existing row needs a reason.
- Rows are deactivated, never deleted.
- A new test, or a test moved to another method, needs an active method version.
- Identity is locked once work depends on it, because reports read method titles, analyte
  names and units live. A method version with a batch, requested test or result keeps its
  code, version, title and source; a test definition with any reference keeps its method,
  analyte, matrix and unit; an analyte used by such a test keeps its name and CAS number.
  Deactivating, the reporting limit and the unlocked fields stay editable, including the
  descriptors (method reference source and technique, analyte group), which only classify.
  To change a locked method, create a new version; the old one stays exactly as results saw it.
- Locks are discovered from the database relations, so new tables that reference the
  catalog lock it automatically. Rows are locked FOR UPDATE, which also serializes against
  a concurrent insert that references them.
"""

import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.roles import has_role, require_role
from core.audit import record_event, snapshot
from core.concurrency import check_original_state

from .models import Analyte, Method, TestDefinition

CATALOG_ROLES = ("admin",)
# Policy tables that may reference a method or test without making it "used work".
CONFIG_TABLES = frozenset(
    {
        "analytical.holdingtimerule",
        "analytical.qcrequirement",
        "analytical.qclimit",
        "analytical.preservationrule",
        "analytical.preparationrequirement",
    }
)
METHOD_IDENTITY = ("code", "version", "title", "source")
DEFINITION_IDENTITY = ("method_id", "analyte_id", "matrix", "unit")
ANALYTE_IDENTITY = ("name", "cas_number")
LIMIT_SCALE = Decimal("0.00000001")
CAS = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")


def can_edit(user):
    return has_role(user, *CATALOG_ROLES)


def _plural(noun):
    if noun.endswith("y") and noun[-2:-1] not in set("aeiou"):
        return noun[:-1] + "ies"
    return noun + ("es" if noun.endswith(("s", "x", "ch", "sh")) else "s")


def _references(obj, ignore=frozenset()):
    """Names of tables holding rows that point at obj (hidden reverse relations included)."""
    found = []
    for rel in obj._meta.get_fields(include_hidden=True):
        if not (rel.auto_created and not rel.concrete and (rel.one_to_one or rel.one_to_many)):
            continue
        label = rel.related_model._meta.label_lower
        if label in ignore:
            continue
        if rel.related_model._base_manager.filter(**{rel.field.name: obj}).exists():
            found.append(_plural(str(rel.related_model._meta.verbose_name)))
    return sorted(set(found))


# Verbose names Django builds for auto-created tables read badly as a "what depends on this" label.
LABEL_OVERRIDES = {"commercial.submissionsample_tests": "Submission sample tests"}


def _label(model):
    if model._meta.label_lower in LABEL_OVERRIDES:
        return LABEL_OVERRIDES[model._meta.label_lower]
    name = _plural(str(model._meta.verbose_name))
    label = name[0].upper() + name[1:]
    for word, shown in (("Qc ", "QC "), ("Mdl ", "MDL "), ("Nysdec ", "NYSDEC ")):
        label = label.replace(word, shown)
    return label


def _reference_counts(model, scope, ignore=frozenset()):
    """{label: rows} pointing at any row of `scope`, a queryset of `model`."""
    counts = {}
    for rel in model._meta.get_fields(include_hidden=True):
        if not (rel.auto_created and not rel.concrete and (rel.one_to_one or rel.one_to_many)):
            continue
        if rel.related_model._meta.label_lower in ignore:
            continue
        rows = rel.related_model._base_manager.filter(**{f"{rel.field.name}__in": scope}).count()
        if rows:
            label = _label(rel.related_model)
            counts[label] = counts.get(label, 0) + rows
    return counts


def usage_facts(obj):
    """(label, value) pairs showing what depends on a method, analyte or test definition."""
    definitions = TestDefinition.objects
    if isinstance(obj, Method):
        scope = definitions.filter(method=obj)
        counts = _reference_counts(
            Method, Method.objects.filter(pk=obj.pk), {"catalog.testdefinition"}
        )
    elif isinstance(obj, Analyte):
        scope = definitions.filter(analyte=obj)
        counts = _reference_counts(  # holding-time rules that name the analyte
            Analyte, Analyte.objects.filter(pk=obj.pk), {"catalog.testdefinition"}
        )
    else:
        scope, counts = definitions.filter(pk=obj.pk), {}
    facts = []
    if not isinstance(obj, TestDefinition):
        total = scope.count()
        facts.append(("Tests", f"{scope.filter(is_active=True).count()} active of {total}"))
    for label, rows in {**counts, **_reference_counts(TestDefinition, scope)}.items():
        facts.append((label, rows))
    return facts


def definition_locks(definition):
    """Why the test definition's identity is locked. QC limits count: they are unit-bound."""
    return _references(definition) if definition.pk else []


def definition_in_work(definition):
    return bool(definition.pk and _references(definition, CONFIG_TABLES))


def method_locks(method):
    if not method.pk:
        return []
    found = _references(method, CONFIG_TABLES | {"catalog.testdefinition"})
    if any(definition_in_work(d) for d in method.testdefinition_set.all()):
        found.append("requested tests or results")
    return sorted(set(found))


def analyte_locks(analyte):
    if not analyte.pk:
        return []
    if any(definition_in_work(d) for d in analyte.testdefinition_set.all()):
        return ["requested tests or results"]
    return []


# ----------------------------------------------------------------------------- helpers


def _clean_text(instance, *names):
    for name in names:
        setattr(instance, name, (getattr(instance, name) or "").strip())


def _required(errors, instance, *names):
    for name in names:
        if not getattr(instance, name):
            errors[name] = "This field is required."


def _check_cas(value):
    match = CAS.match(value)
    if not match:
        return "Use the CAS format, for example 7439-92-1."
    digits = (match.group(1) + match.group(2))[::-1]
    if sum(int(d) * (i + 1) for i, d in enumerate(digits)) % 10 != int(match.group(3)):
        return "This CAS number's check digit is wrong."
    return None


def _lock_violations(instance, old, names, reasons):
    errors = {}
    for name in names:
        if getattr(instance, name) != getattr(old, name):
            errors[name.removesuffix("_id")] = (
                f"Locked: used by {', '.join(reasons)}. Create a new version or deactivate this."
            )
    return errors


def _authorize_change(instance, reason, old, lock_reasons, identity):
    """Reason and edit-lock checks for a change to an existing row; returns the clean reason."""
    reason = (reason or "").strip()
    errors = {}
    if old is not None and snapshot(instance) != snapshot(old):
        if not reason:
            errors["reason"] = "Give the reason for this change."
        if lock_reasons:
            errors |= _lock_violations(instance, old, identity, lock_reasons)
    if errors:
        raise ValidationError(errors)
    return reason


def _lock(model, pk):
    row = model.objects.select_for_update().filter(pk=pk).first()
    if row is None:
        raise ValidationError("This record no longer exists.")
    return row


def _audit_save(actor, instance, old, reason, noun):
    before = snapshot(old) if old else {}
    instance.full_clean()
    if old and snapshot(instance) == before:
        return instance, False
    instance.save()
    record_event(
        actor=actor,
        action=f"catalog.{noun}.{'updated' if old else 'created'}",
        obj=instance,
        before=before,
        after=snapshot(instance),
        reason=reason,
    )
    return instance, True


# -------------------------------------------------------------------------- methods


@transaction.atomic
def save_method(*, actor, instance, reason="", expected_state=None):
    """Create or change a method version. Returns (method, changed)."""
    require_role(actor, *CATALOG_ROLES)
    _clean_text(instance, "code", "version", "title", "source")
    old = None
    reasons = []
    if instance.pk:
        old = _lock(Method, instance.pk)
        check_original_state(old, expected_state)
        list(TestDefinition.objects.select_for_update().filter(method_id=instance.pk))
        reasons = method_locks(old)
    errors = {}
    _required(errors, instance, "code", "version", "title")
    clash = Method.objects.filter(code__iexact=instance.code, version__iexact=instance.version)
    if clash.exclude(pk=instance.pk).exists():
        errors["version"] = "This method already has this version."
    if errors:
        raise ValidationError(errors)
    reason = _authorize_change(instance, reason, old, reasons, METHOD_IDENTITY)
    return _audit_save(actor, instance, old, reason, "method")


@transaction.atomic
def create_method_version(
    *,
    actor,
    previous,
    version,
    title,
    source,
    copy_tests,
    retire_previous,
    reason,
    reference=None,
    technique=None,
):
    """New version of a method: same code, new version, optionally with the active tests
    copied and the previous version retired. The previous version is not altered otherwise.
    Reference and technique default to the previous version's values."""
    require_role(actor, *CATALOG_ROLES)
    previous = _lock(Method, previous.pk)
    if previous is None:
        raise ValidationError("The method no longer exists.")
    reason = (reason or "").strip()
    errors = {}
    if not reason:
        errors["reason"] = "Say why a new version is needed."
    new = Method(
        code=previous.code,
        version=(version or "").strip(),
        title=(title or "").strip(),
        source=(source or "").strip(),
        reference=previous.reference if reference is None else reference,
        technique=previous.technique if technique is None else technique,
    )
    _required(errors, new, "version", "title")
    if Method.objects.filter(code__iexact=new.code, version__iexact=new.version).exists():
        errors["version"] = "This method already has this version."
    if errors:
        raise ValidationError(errors)
    new.full_clean()
    new.save()
    record_event(
        actor=actor,
        action="catalog.method.created",
        obj=new,
        after={**snapshot(new), "derived_from": previous.pk},
        reason=reason,
    )
    if copy_tests:
        sources = previous.testdefinition_set.filter(is_active=True).order_by("pk")
        for old in sources:
            copy = TestDefinition.objects.create(
                method=new,
                analyte=old.analyte,
                matrix=old.matrix,
                unit=old.unit,
                reporting_limit=old.reporting_limit,
            )
            record_event(
                actor=actor,
                action="catalog.testdefinition.created",
                obj=copy,
                after={**snapshot(copy), "copied_from": old.pk},
                reason=reason,
            )
    if retire_previous and previous.is_active:
        before = snapshot(previous)
        previous.is_active = False
        previous.save(update_fields=["is_active"])
        record_event(
            actor=actor,
            action="catalog.method.updated",
            obj=previous,
            before=before,
            after=snapshot(previous),
            reason=f"Retired by new version {new.version}: {reason}",
        )
    return new


# -------------------------------------------------------------------------- analytes


@transaction.atomic
def save_analyte(*, actor, instance, reason="", expected_state=None):
    require_role(actor, *CATALOG_ROLES)
    _clean_text(instance, "name", "cas_number")
    old = None
    reasons = []
    if instance.pk:
        old = _lock(Analyte, instance.pk)
        check_original_state(old, expected_state)
        list(TestDefinition.objects.select_for_update().filter(analyte_id=instance.pk))
        reasons = analyte_locks(old)
    errors = {}
    _required(errors, instance, "name")
    if Analyte.objects.filter(name__iexact=instance.name).exclude(pk=instance.pk).exists():
        errors["name"] = "An analyte with this name already exists."
    if instance.cas_number:
        problem = _check_cas(instance.cas_number)
        if problem:
            errors["cas_number"] = problem
    if errors:
        raise ValidationError(errors)
    reason = _authorize_change(instance, reason, old, reasons, ANALYTE_IDENTITY)
    return _audit_save(actor, instance, old, reason, "analyte")


# ------------------------------------------------------------------- test definitions


@transaction.atomic
def save_test_definition(*, actor, instance, reason="", expected_state=None):
    require_role(actor, *CATALOG_ROLES)
    _clean_text(instance, "unit")
    old = None
    reasons = []
    if instance.pk:
        old = _lock(TestDefinition, instance.pk)
        check_original_state(old, expected_state)
        reasons = definition_locks(old)
    errors = {}
    _required(errors, instance, "unit")
    limit = instance.reporting_limit
    if limit is not None:
        if not limit.is_finite() or limit <= 0:
            errors["reporting_limit"] = "The reporting limit must be greater than zero, or blank."
        elif limit != limit.quantize(LIMIT_SCALE):
            errors["reporting_limit"] = "Use at most 8 decimal places."
        else:  # same stored scale as the database, so an unchanged value compares equal
            instance.reporting_limit = limit.quantize(LIMIT_SCALE)
    # New work needs an active method version: the form offers only active versions for a new
    # test or a changed method; an existing test on a since-retired version stays editable.
    if (
        instance.method_id
        and (old is None or instance.method_id != old.method_id)
        and not Method.objects.filter(pk=instance.method_id, is_active=True).exists()
    ):
        errors["method"] = "This method version is inactive. Choose an active version."
    clash = TestDefinition.objects.filter(
        method_id=instance.method_id, analyte_id=instance.analyte_id, matrix=instance.matrix
    )
    if clash.exclude(pk=instance.pk).exists():
        errors["analyte"] = "This method already has a test for this analyte and matrix."
    if errors:
        raise ValidationError(errors)
    reason = _authorize_change(instance, reason, old, reasons, DEFINITION_IDENTITY)
    return _audit_save(actor, instance, old, reason, "testdefinition")
