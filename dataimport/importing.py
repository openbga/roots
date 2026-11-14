"""Spreadsheet import of setup records: one CSV file per record type, all rows or none.

Every row goes through the owning domain service (catalog.services.save_*,
analytical.services.save_config, receipt.services.create_*/update_*), so validation, identity
locks, role checks and audit events are those of manual entry. This module reads the file, finds
the record each row names by its natural key, converts cells exactly (Decimal from the cell text,
never a float) and translates the services' errors onto the row. It never writes a model itself.

Preview runs the same service calls as commit inside a transaction that is always rolled back, so
it reports exactly what commit would do and stores nothing. (Sequence numbers drawn by the
rolled-back inserts are not reused; record IDs may therefore skip.) Commit parses the file again,
refuses it if the records it touches changed since the preview, writes every row in one
transaction and records one summary event for the file. Any row error writes nothing.
"""

import csv
import hashlib
import io
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.validators import DecimalValidator
from django.db import DatabaseError, connection, transaction

from accounts.roles import has_role, require_role
from analytical import importing as result_import
from analytical import services as analytical_services
from analytical.forms import hours_field
from analytical.models import HoldingTimeRule, PreservationRule, QCLimit, QCRequirement
from catalog import services as catalog_services
from catalog.models import Analyte, Method, TestDefinition
from core.concurrency import record_token
from core.models import AuditEvent
from receipt import services as receipt_services
from receipt.models import Client, Project

# The same bounds as the results import.
MAX_BYTES = result_import.MAX_BYTES
MAX_ROWS = result_import.MAX_ROWS
SUMMARY_ACTION = "dataimport.file_imported"
PREVIEW_REASON = "Preview"  # stands in for the reason during the dry run; never stored

# ASCII digits only: \d alone also matches other scripts' digits, which Decimal() accepts.
_DECIMAL = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)", re.ASCII)
_INTEGER = re.compile(r"\d{1,9}", re.ASCII)
logger = logging.getLogger(__name__)
_BOOLEAN = {"yes": True, "no": False, "true": True, "false": False}
_HOURS = hours_field("Hours", False, "")


class RowError(Exception):
    def __init__(self, *messages):
        super().__init__(*messages)
        self.messages = list(messages)


@dataclass(frozen=True)
class Column:
    """One CSV column. Data columns name a model attribute; key columns identify the record."""

    name: str
    help: str
    attr: str = ""  # model attribute for a data column; "" for a key column
    kind: str = "text"  # text | choice | decimal | integer | boolean | hours | user
    key: bool = False
    choices: tuple = ()  # allowed values of a key column (data columns read the model field)
    optional: bool = False  # a key column that may be blank or left out of the file


@dataclass(frozen=True)
class RecordType:
    slug: str
    title: str  # plural, as on the index: "Clients"
    noun: str
    area: str
    model: type
    roles: tuple
    columns: tuple
    needs: str
    list_route: str
    find: Callable  # (keys) -> (existing or None, fields for a new record, label)
    write: Callable  # (record_type, actor, existing, new_fields, data, reason, state) -> None
    check: Callable | None = None  # (existing, data) -> extra row errors before writing

    @property
    def key_columns(self):
        return [c for c in self.columns if c.key]

    @property
    def data_columns(self):
        return [c for c in self.columns if not c.key]

    def model_field(self, column):
        return self.model._meta.get_field(column.attr)

    def required_on_create(self, column):
        if column.key:
            return True
        model_field = self.model_field(column)
        return not model_field.blank and not model_field.has_default()

    def allowed(self, column):
        if column.key:
            return [value for value, _ in column.choices]
        return [value for value, _ in self.model_field(column).choices or ()]

    def column_for(self, name):
        """The CSV column a service error key refers to (model field name or attname)."""
        for column in self.columns:
            if name and name in (column.attr, column.attr.removesuffix("_id"), column.name):
                return column.name
        return KEY_ERROR_COLUMNS.get(name, name)

    def describe(self, column):
        """(requirement, format) for the column documentation and the template notes."""
        if column.key:
            requirement = "Key, optional" if column.optional else "Key, required"
        elif self.required_on_create(column):
            requirement = "Required for a new record; cannot be blank"
        elif column.kind == "boolean":
            requirement = "Optional; cannot be blank"
        elif not self.model_field(column).blank:
            requirement = "Optional; cannot be blank"
        else:
            requirement = "Optional; blank clears it"
        allowed = self.allowed(column)
        if allowed:
            return requirement, "One of: " + ", ".join(allowed)
        if column.kind == "decimal":
            model_field = self.model_field(column)
            return requirement, (
                f"Plain decimal number (digits 0-9), at most {model_field.max_digits} digits, "
                f"{model_field.decimal_places} of them after the decimal point"
            )
        if column.kind == "integer":
            return requirement, "Whole number (digits 0-9)"
        if column.kind == "boolean":
            return requirement, "yes or no"
        if column.kind == "hours":
            return requirement, (
                "Hours as a decimal number (digits 0-9), at most 2 decimal places, at least 0.01"
            )
        if column.kind == "user":
            return requirement, "Username of an active laboratory staff account"
        max_length = 0 if column.key else self.model_field(column).max_length
        return requirement, f"Text, up to {max_length} characters" if max_length else "Text"


# Service errors keyed by a model field that a key column supplies.
KEY_ERROR_COLUMNS = {
    "method": "method_code",
    "definition": "analyte",
    "code": "method_code",
    "version": "method_version",
    "account_number": "account_code",
    "client": "client_account_code",
}


# ------------------------------------------------------------------------------- file model


@dataclass
class Row:
    line: int
    values: dict  # column name -> cell text, stripped of surrounding whitespace
    label: str = ""
    action: str = ""  # create | update | unchanged; blank when the row could not be read
    existing: object = None
    new_fields: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)  # model attribute -> converted value
    changes: list = field(default_factory=list)  # (column, before, after) display text
    errors: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors


@dataclass
class Preview:
    record_type: RecordType
    filename: str
    file_hash: str = ""
    file_bytes: int = 0
    rows: list = field(default_factory=list)
    file_errors: list = field(default_factory=list)
    state_token: str = ""

    @property
    def ok(self):
        return not self.file_errors and bool(self.rows) and all(r.ok for r in self.rows)

    @property
    def error_rows(self):
        return [r for r in self.rows if r.errors]

    @property
    def error_count(self):
        return len(self.file_errors) + sum(len(r.errors) for r in self.rows)

    @property
    def counts(self):
        counted = Counter(r.action for r in self.rows if r.ok)
        return {
            "create": counted["create"],
            "update": counted["update"],
            "unchanged": counted["unchanged"],
            "error": len(self.error_rows),
        }

    @property
    def needs_reason(self):
        return any(r.action == "update" for r in self.rows)

    @property
    def ordered_rows(self):
        """Rows with errors first, so the first problem is at the top; then file order."""
        return self.error_rows + [r for r in self.rows if not r.errors]


# --------------------------------------------------------------------------- cell parsing


def _cell(record_type, column, text):
    """Converted value of a nonblank data cell. Raises RowError naming the column."""
    name = column.name
    if column.kind == "choice":
        allowed = record_type.allowed(column)
        if text not in allowed:
            raise RowError(f"{name}: '{text}' is not allowed. Use one of: {', '.join(allowed)}.")
        return text
    if column.kind == "decimal":
        if not _DECIMAL.fullmatch(text):
            raise RowError(
                f"{name}: '{text}' is not a plain decimal number. Use digits with an optional "
                "decimal point; no exponent, thousands separator, unit or symbol."
            )
        value = Decimal(text)
        model_field = record_type.model_field(column)
        try:  # the bounds the manual form applies before the service sees the value
            DecimalValidator(model_field.max_digits, model_field.decimal_places)(value)
        except ValidationError as exc:
            raise RowError(*(f"{name}: {message}" for message in exc.messages)) from None
        return value
    if column.kind == "integer":
        if not _INTEGER.fullmatch(text):
            raise RowError(f"{name}: '{text}' is not a whole number.")
        return int(text)
    if column.kind == "boolean":
        if text.casefold() not in _BOOLEAN:
            raise RowError(f"{name}: '{text}' is not yes or no.")
        return _BOOLEAN[text.casefold()]
    if column.kind == "hours":
        if not _DECIMAL.fullmatch(text):
            raise RowError(f"{name}: '{text}' is not a number of hours, for example 48 or 0.25.")
        try:
            hours = _HOURS.clean(text)
        except ValidationError as exc:
            raise RowError(*(f"{name}: {message}" for message in exc.messages)) from None
        # The same conversion as the manual form: hours to whole seconds.
        seconds = (hours * 3600).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return timedelta(seconds=int(seconds))
    if column.kind == "user":
        user = get_user_model().objects.filter(username=text).first()
        if user is None:
            raise RowError(f"{name}: no account with username '{text}'.")
        return user
    return text


def _key_choice(column, text):
    allowed = [value for value, _ in column.choices]
    if text not in allowed:
        raise RowError(f"{column.name}: '{text}' is not allowed. Use one of: {', '.join(allowed)}.")
    return text


def show(value):
    """Display text for a value in the preview (never a float, never padded)."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, timedelta):
        hours = Decimal(int(value.total_seconds())) / 3600
        return f"{format(hours.quantize(Decimal('0.01')).normalize(), 'f')} h"
    if hasattr(value, "get_username"):
        return value.get_username()
    return str(value)


def _same(old, new):
    if hasattr(old, "pk") and hasattr(new, "pk"):
        return old.pk == new.pk
    return old == new


# ------------------------------------------------------------------------------ lookups


def _one(queryset, what):
    found = list(queryset[:2])
    if len(found) > 1:
        raise RowError(f"{what} matches more than one existing record; fix the records first.")
    return found[0] if found else None


def _method(keys):
    code, version = keys["method_code"], keys["method_version"]
    method = _one(
        Method.objects.filter(code__iexact=code, version__iexact=version),
        f"Method {code} version {version}",
    )
    if method is None:
        raise RowError(
            f"method_code, method_version: no method {code} version {version}. "
            "Import or create the method first."
        )
    return method


def _analyte(name):
    analyte = _one(Analyte.objects.filter(name__iexact=name), f"Analyte {name}")
    if analyte is None:
        raise RowError(f"analyte: no analyte named '{name}'. Import or create it first.")
    return analyte


def _test(keys):
    errors, method, analyte = [], None, None
    for resolve in (lambda: _method(keys), lambda: _analyte(keys["analyte"])):
        try:
            found = resolve()
        except RowError as exc:
            errors += exc.messages
        else:
            method, analyte = (found, analyte) if isinstance(found, Method) else (method, found)
    try:
        matrix = _key_choice(MATRIX, keys["matrix"])
    except RowError as exc:
        errors += exc.messages
    if errors:
        raise RowError(*errors)
    test = TestDefinition.objects.filter(method=method, analyte=analyte, matrix=matrix).first()
    return method, analyte, matrix, test


def _test_label(keys):
    return f"{keys['analyte']} · {keys['method_code']} {keys['method_version']} · {keys['matrix']}"


def find_analyte(keys):
    existing = _one(Analyte.objects.filter(name__iexact=keys["name"]), f"Analyte {keys['name']}")
    return existing, {"name": keys["name"]}, keys["name"]


def find_method(keys):
    code, version = keys["method_code"], keys["method_version"]
    existing = _one(
        Method.objects.filter(code__iexact=code, version__iexact=version),
        f"Method {code} version {version}",
    )
    return existing, {"code": code, "version": version}, f"{code} {version}"


def find_test(keys):
    method, analyte, matrix, test = _test(keys)
    return test, {"method": method, "analyte": analyte, "matrix": matrix}, _test_label(keys)


def find_qc_requirement(keys):
    errors = []
    try:
        method = _method(keys)
    except RowError as exc:
        errors += exc.messages
    try:
        qc_type = _key_choice(QC_TYPE, keys["qc_type"])
    except RowError as exc:
        errors += exc.messages
    if errors:
        raise RowError(*errors)
    existing = QCRequirement.objects.filter(method=method, qc_type=qc_type).first()
    label = f"{keys['method_code']} {keys['method_version']} · {qc_type}"
    return existing, {"method": method, "qc_type": qc_type}, label


def find_holding_rule(keys):
    """A blank analyte is part of the key: it names the method-wide rule, never an analyte's."""
    errors, method, analyte, chosen = [], None, None, {}
    try:
        method = _method(keys)
    except RowError as exc:
        errors += exc.messages
    if keys["analyte"]:
        try:
            analyte = _analyte(keys["analyte"])
        except RowError as exc:
            errors += exc.messages
    for column in (MATRIX, START_EVENT, END_EVENT):
        try:
            chosen[column.name] = _key_choice(column, keys[column.name])
        except RowError as exc:
            errors += exc.messages
    if errors:
        raise RowError(*errors)
    existing = HoldingTimeRule.objects.filter(method=method, analyte=analyte, **chosen).first()
    label = (
        f"{keys['method_code']} {keys['method_version']} · {chosen['matrix']} · "
        f"{analyte.name if analyte else 'all analytes'} · "
        f"{chosen['start_event']} → {chosen['end_event']}"
    )
    return existing, {"method": method, "analyte": analyte, **chosen}, label


def find_qc_limit(keys):
    errors, test, chosen = [], None, {}
    try:
        _, _, _, test = _test(keys)
        if test is None:
            errors.append(
                f"analyte: no test for {_test_label(keys)}. Import or create the test first."
            )
    except RowError as exc:
        errors += exc.messages
    for column in (PHASE, QC_TYPE, MEASURE):
        try:
            chosen[column.name] = _key_choice(column, keys[column.name])
        except RowError as exc:
            errors += exc.messages
    if errors:
        raise RowError(*errors)
    existing = QCLimit.objects.filter(definition=test, **chosen).first()
    label = f"{_test_label(keys)} · {chosen['phase']} · {chosen['qc_type']} · {chosen['measure']}"
    return existing, {"definition": test, **chosen}, label


def find_preservation_rule(keys):
    _, _, _, test = _test(keys)
    if test is None:
        raise RowError(
            f"analyte: no test for {_test_label(keys)}. Import or create the test first."
        )
    existing = PreservationRule.objects.filter(definition=test).first()
    return existing, {"definition": test}, _test_label(keys)


def find_client(keys):
    code = keys["account_code"]
    existing = Client.objects.filter(account_number=code).first()
    return existing, {"account_number": code}, code


def check_client(existing, data):
    """Clients have no other dependable key: a new account code beside an existing client of the
    same name would silently create a second client, so it is an error instead."""
    name = data.get("name")
    if existing is not None or not name:
        return []
    twins = list(Client.objects.filter(name__iexact=name).values_list("account_number", flat=True))
    if not twins:
        return []
    codes = ", ".join(code or "no account code" for code in twins)
    return [
        f"name: a client named '{name}' already exists ({codes}). Use its account code in this "
        "row, or give it this account code on the client page first."
    ]


def find_project(keys):
    code, name = keys["client_account_code"], keys["name"]
    client = Client.objects.filter(account_number=code).first()
    if client is None:
        raise RowError(
            f"client_account_code: no client with account code '{code}'. Import or create the "
            "client first, or give the client this account code."
        )
    existing = _one(
        Project.objects.filter(client=client, name__iexact=name),
        f"Project '{name}' of client {code}",
    )
    return existing, {"client": client, "name": name}, f"{code} · {name}"


# ------------------------------------------------------------------------------ writers


def _fresh(record_type, existing, new_fields, data):
    instance = (
        record_type.model.objects.get(pk=existing.pk)
        if existing
        else record_type.model(**new_fields)
    )
    for attr, value in data.items():
        setattr(instance, attr, value)
    return instance


def write_catalog(save):
    def write(record_type, actor, existing, new_fields, data, reason, state):
        save(
            actor=actor,
            instance=_fresh(record_type, existing, new_fields, data),
            reason=reason,
            expected_state=state,
        )

    return write


def write_config(record_type, actor, existing, new_fields, data, reason, state):
    analytical_services.save_config(
        actor=actor,
        instance=_fresh(record_type, existing, new_fields, data),
        reason=reason,
        expected_state=state,
    )


def write_client(record_type, actor, existing, new_fields, data, reason, state):
    if existing is None:
        # Inside the transaction, so a client created by an earlier row of this file counts too.
        twins = check_client(None, data)
        if twins:
            raise ValidationError(twins)
        receipt_services.create_client(actor=actor, reason=reason, **new_fields, **data)
    else:
        receipt_services.update_client(
            actor=actor,
            client_id=existing.pk,
            expected_revision=existing.revision,
            reason=reason,
            **data,
        )


def write_project(record_type, actor, existing, new_fields, data, reason, state):
    if existing is None:
        # Projects have no unique key: lock the client, then look again, so two imports of one
        # file cannot both create the project after reading that it does not exist.
        client = Client.objects.select_for_update().get(pk=new_fields["client"].pk)
        name = new_fields["name"]
        if Project.objects.filter(client=client, name__iexact=name).exists():
            raise ValidationError(
                {
                    "name": f"Client {client.account_number} now has a project named '{name}'. "
                    "Upload the file again."
                }
            )
        receipt_services.create_project(actor=actor, reason=reason, **new_fields, **data)
    else:
        receipt_services.update_project(
            actor=actor,
            project_id=existing.pk,
            expected_revision=existing.revision,
            reason=reason,
            **data,
        )


# ------------------------------------------------------------------------ specifications


def _choices(model, name):
    return tuple(model._meta.get_field(name).choices)


MATRIX = Column(
    "matrix", "Matrix of the test.", key=True, choices=_choices(TestDefinition, "matrix")
)
QC_TYPE = Column("qc_type", "QC type.", key=True, choices=_choices(QCLimit, "qc_type"))
PHASE = Column(
    "phase",
    "Whether the limit applies to analysis QC or preparation QC.",
    key=True,
    choices=_choices(QCLimit, "phase"),
)
MEASURE = Column(
    "measure",
    "The quantity the limit applies to: measured value, percent recovery or relative percent "
    "difference.",
    key=True,
    choices=_choices(QCLimit, "measure"),
)
START_EVENT = Column(
    "start_event",
    "Event that starts the clock.",
    key=True,
    choices=_choices(HoldingTimeRule, "start_event"),
)
END_EVENT = Column(
    "end_event",
    "Event that stops the clock.",
    key=True,
    choices=_choices(HoldingTimeRule, "end_event"),
)
METHOD_CODE = Column(
    "method_code", "Code of an existing method version, for example EPA 200.8.", key=True
)
METHOD_VERSION = Column("method_version", "Version of that method, as recorded.", key=True)
ANALYTE = Column("analyte", "Name of an existing analyte.", key=True)
RULE_ANALYTE = Column(
    "analyte",
    "Name of an existing analyte with a test of this method and matrix (or a test this method "
    "prepares), to limit the rule to it. Blank or left out: the rule applies to every analyte "
    "of the method. An analyte's rule replaces the method-wide rule with the same start and end "
    "events for that analyte.",
    key=True,
    optional=True,
)
TEST_KEYS = (METHOD_CODE, METHOD_VERSION, ANALYTE, MATRIX)
ACTIVE = Column(
    "active",
    "Whether the record is available for new work. A new record is active unless this says no.",
    "is_active",
    "boolean",
)
BASIS = Column("basis", "Method or SOP reference for this rule.", "basis")

RECEIPT_ROLES = ("project_manager",)

TYPES = (
    RecordType(
        "clients",
        "Clients",
        "client",
        "Intake",
        Client,
        RECEIPT_ROLES,
        (
            Column(
                "account_code",
                "The laboratory's short code for the client. It identifies the client in this "
                "file and in the projects file; it is required here although the client form "
                "leaves it optional.",
                key=True,
            ),
            Column("name", "The organization as it should appear on reports.", "name"),
            Column("contact_name", "The person reports are addressed to.", "contact_name"),
            Column("email", "Email address of that contact.", "email"),
            Column("phone", "Phone number of that contact.", "phone"),
            Column("address_line1", "Street address.", "address_line1"),
            Column("address_line2", "Suite, building or PO box.", "address_line2"),
            Column("city", "City.", "city"),
            Column("state", "Two-letter US state code.", "state", "choice"),
            Column("postal_code", "ZIP code.", "postal_code"),
            Column("billing_contact_name", "Invoice-to contact.", "billing_contact_name"),
            Column("billing_email", "Invoice email.", "billing_email"),
            Column(
                "billing_address",
                "Invoice address when invoices do not go to the mailing address.",
                "billing_address",
            ),
            Column("payment_terms", "Payment terms.", "payment_terms", "choice"),
            Column(
                "po_required",
                "Whether a chain of custody for this client should carry a purchase order.",
                "po_required",
                "boolean",
            ),
            Column("report_preferences", "Report instructions.", "report_preferences"),
            Column("notes", "Internal notes; not shown to the client.", "notes"),
            ACTIVE,
        ),
        "Nothing. Import clients before their projects.",
        "receipt:client-list",
        find_client,
        write_client,
        check_client,
    ),
    RecordType(
        "projects",
        "Projects",
        "project",
        "Intake",
        Project,
        RECEIPT_ROLES,
        (
            Column(
                "client_account_code",
                "Account code of an existing client. A project's client never changes.",
                key=True,
            ),
            Column(
                "name",
                "Project name. With the client it identifies the project; to rename a project, "
                "use its edit form.",
                key=True,
            ),
            Column("number", "The client's or the laboratory's reference.", "number"),
            Column("regulatory_program", "Regulatory program.", "regulatory_program", "choice"),
            Column("program", "Permit or program ID.", "program"),
            Column("site_address", "Site or facility where samples are collected.", "site_address"),
            Column(
                "site_state", "State of sample origin, two-letter code.", "site_state", "choice"
            ),
            Column("po_number", "Standing purchase order.", "po_number"),
            Column(
                "project_manager",
                "Laboratory project manager. Blank clears it.",
                "project_manager",
                "user",
            ),
            Column("turnaround_days", "Turnaround in days.", "turnaround_days", "integer"),
            Column(
                "instructions",
                "Standing instructions shown to the custodian at receipt.",
                "instructions",
            ),
            ACTIVE,
        ),
        "Clients with account codes, and the project managers' accounts.",
        "receipt:project-list",
        find_project,
        write_project,
    ),
    RecordType(
        "analytes",
        "Analytes",
        "analyte",
        "Catalog",
        Analyte,
        catalog_services.CATALOG_ROLES,
        (
            Column(
                "name",
                "Analyte name. It identifies the analyte (case is ignored); to rename an analyte, "
                "use its edit form.",
                key=True,
            ),
            Column(
                "cas_number",
                "CAS number, for example 7439-92-1. The check digit is verified.",
                "cas_number",
            ),
            Column("group", "Analyte group, used to filter the catalog.", "group", "choice"),
        ),
        "Nothing. Import analytes and methods before tests.",
        "catalog:analyte-list",
        find_analyte,
        write_catalog(catalog_services.save_analyte),
    ),
    RecordType(
        "methods",
        "Methods",
        "method version",
        "Catalog",
        Method,
        catalog_services.CATALOG_ROLES,
        (
            Column("method_code", "Method code, for example EPA 200.8.", key=True),
            Column(
                "method_version",
                "Revision, edition or year, exactly as the laboratory names it. Code and version "
                "together identify the method version (case is ignored).",
                key=True,
            ),
            Column("title", "Method title.", "title"),
            Column("source", "Citation, SOP number and revision.", "source"),
            Column("reference", "Who publishes the method.", "reference", "choice"),
            Column("technique", "Measurement technique.", "technique", "choice"),
            ACTIVE,
        ),
        "Nothing. Import analytes and methods before tests.",
        "catalog:method-list",
        find_method,
        write_catalog(catalog_services.save_method),
    ),
    RecordType(
        "tests",
        "Tests",
        "test",
        "Catalog",
        TestDefinition,
        catalog_services.CATALOG_ROLES,
        (
            *TEST_KEYS,
            Column(
                "unit",
                "Result unit, stored exactly as written and never converted.",
                "unit",
            ),
            Column(
                "reporting_limit",
                "Default reporting limit in the unit above. Blank means none.",
                "reporting_limit",
                "decimal",
            ),
            ACTIVE,
        ),
        "The analytes and method versions the tests name.",
        "catalog:test-list",
        find_test,
        write_catalog(catalog_services.save_test_definition),
    ),
    RecordType(
        "qc-requirements",
        "QC requirements",
        "QC requirement",
        "QC configuration",
        QCRequirement,
        analytical_services.CONFIG_ROLES,
        (
            METHOD_CODE,
            METHOD_VERSION,
            QC_TYPE,
            Column("min_per_batch", "Minimum per batch.", "min_per_batch", "integer"),
            Column(
                "every_n_samples",
                "Also one per this many field samples. Blank means none.",
                "every_n_samples",
                "integer",
            ),
            BASIS,
            ACTIVE,
        ),
        "The method versions the requirements name.",
        "analytical:qc-config",
        find_qc_requirement,
        write_config,
    ),
    RecordType(
        "holding-rules",
        "Holding-time rules",
        "holding-time rule",
        "QC configuration",
        HoldingTimeRule,
        analytical_services.CONFIG_ROLES,
        (
            METHOD_CODE,
            METHOD_VERSION,
            MATRIX,
            RULE_ANALYTE,
            START_EVENT,
            END_EVENT,
            Column("name", "Rule name shown on the holding-time clock.", "name"),
            Column(
                "duration_hours",
                "Maximum time from the start event to the end event, in hours (28 days = 672).",
                "duration",
                "hours",
            ),
            Column(
                "warning_hours",
                "Flag as at risk when this much time or less remains. Blank means no warning.",
                "warning_window",
                "hours",
            ),
            BASIS,
            ACTIVE,
        ),
        "The method versions the rules name, and the tests of any analyte a rule names.",
        "analytical:qc-config",
        find_holding_rule,
        write_config,
    ),
    RecordType(
        "qc-limits",
        "QC limits",
        "QC limit",
        "QC configuration",
        QCLimit,
        analytical_services.CONFIG_ROLES,
        (
            *TEST_KEYS,
            PHASE,
            QC_TYPE,
            MEASURE,
            Column(
                "lower",
                "Lower acceptance limit: the result unit for a measured value, percent for "
                "recovery and RPD. Give a lower limit, an upper limit or both.",
                "lower",
                "decimal",
            ),
            Column("upper", "Upper acceptance limit, in the same unit.", "upper", "decimal"),
            BASIS,
            ACTIVE,
        ),
        "The tests the limits name.",
        "analytical:qc-config",
        find_qc_limit,
        write_config,
    ),
    RecordType(
        "preservation-rules",
        "Preservation rules",
        "preservation rule",
        "QC configuration",
        PreservationRule,
        analytical_services.CONFIG_ROLES,
        (
            *TEST_KEYS,
            Column(
                "temperature_mode",
                "How the recorded cooler temperature is screened.",
                "temperature_mode",
                "choice",
            ),
            Column(
                "temperature_lower",
                "Minimum cooler receipt temperature, only with range.",
                "lower",
                "decimal",
            ),
            Column(
                "temperature_upper",
                "Maximum cooler receipt temperature, only with range.",
                "upper",
                "decimal",
            ),
            Column("temperature_unit", "Temperature unit.", "unit", "choice"),
            Column(
                "container_requirements",
                "Container requirements, checked by people.",
                "container_requirements",
            ),
            Column(
                "chemical_requirements",
                "Chemical preservation requirements, checked by people.",
                "chemical_requirements",
            ),
            Column("other_requirements", "Other requirements.", "other_requirements"),
            Column(
                "basis",
                "Applicable method or SOP, program scope and source revision. Printed on issued "
                "reports.",
                "basis",
            ),
            ACTIVE,
        ),
        "The tests the rules name.",
        "analytical:qc-config",
        find_preservation_rule,
        write_config,
    ),
)
BY_SLUG = {record_type.slug: record_type for record_type in TYPES}


def allowed_types(user):
    """Record types this account may import, in import order."""
    return [record_type for record_type in TYPES if has_role(user, *record_type.roles)]


def template_bytes(record_type):
    """Header row of a blank template, UTF-8 with a byte-order mark for spreadsheet programs."""
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator="\r\n").writerow(c.name for c in record_type.columns)
    return ("﻿" + out.getvalue()).encode("utf-8")


# ------------------------------------------------------------------------------- parsing


def _read_header(record_type, header, preview):
    names = [h.strip().lower() for h in header]
    allowed = [c.name for c in record_type.columns]
    blank = [str(i) for i, name in enumerate(names, start=1) if not name]
    unknown = [name for name in names if name and name not in allowed]
    duplicate = sorted({name for name in names if name and names.count(name) > 1})
    missing = [c.name for c in record_type.key_columns if c.name not in names and not c.optional]
    if blank:
        preview.file_errors.append(f"Line 1: column {', '.join(blank)} has no name.")
    if unknown:
        preview.file_errors.append(
            f"Line 1: unknown column(s) {', '.join(unknown)}. Allowed: {', '.join(allowed)}."
        )
    if duplicate:
        preview.file_errors.append(f"Line 1: duplicate column(s) {', '.join(duplicate)}.")
    if missing:
        preview.file_errors.append(f"Line 1: missing key column(s) {', '.join(missing)}.")
    return names


def _identity(row):
    if row.existing is not None:
        return ("existing", row.existing.pk)
    return tuple(
        sorted(
            (name, value.pk if hasattr(value, "pk") else str(value).casefold())
            for name, value in row.new_fields.items()
        )
    )


def _read_row(record_type, row, present, seen):
    keys = {}
    for column in record_type.key_columns:
        keys[column.name] = row.values.get(column.name, "")  # an optional key may be left out
        if not keys[column.name] and not column.optional:
            row.errors.append(f"{column.name}: required; it identifies the record.")
    data = {}  # only columns present in the file: an absent column is left alone
    for column in record_type.data_columns:
        if column.name not in present:
            continue
        text = row.values[column.name]
        if text:
            try:
                data[column.attr] = _cell(record_type, column, text)
            except RowError as exc:
                row.errors += exc.messages
            continue
        model_field = record_type.model_field(column)
        if column.kind == "boolean" or not model_field.blank:
            row.errors.append(f"{column.name}: cannot be blank.")
        else:
            data[column.attr] = None if model_field.null else ""
    if row.errors:
        return
    try:
        row.existing, row.new_fields, row.label = record_type.find(keys)
    except RowError as exc:
        row.errors += exc.messages
        return
    identity = _identity(row)
    if identity in seen:
        row.errors.append(f"Duplicate of line {seen[identity]}: one row per record.")
        return
    seen[identity] = row.line
    row.data = data
    if row.existing is None:
        row.action = "create"
        for column in record_type.data_columns:
            if column.name not in present and record_type.required_on_create(column):
                row.errors.append(
                    f"{column.name}: required for a new {record_type.noun}; "
                    "the column is missing from the file."
                )
            elif column.attr in data and show(data[column.attr]) != "":
                row.changes.append((column.name, "", show(data[column.attr])))
    else:
        for column in record_type.data_columns:
            if column.attr not in data:
                continue
            before = getattr(row.existing, column.attr)
            if not _same(before, data[column.attr]):
                row.changes.append((column.name, show(before), show(data[column.attr])))
        row.action = "update" if row.changes else "unchanged"
    if record_type.check and not row.errors:
        row.errors += record_type.check(row.existing, data)


def parse(record_type, data, filename):
    """Read and resolve the uploaded bytes. Reads the database; writes nothing."""
    preview = Preview(
        record_type,
        filename=filename,
        file_hash=result_import.file_digest(data),
        file_bytes=len(data),
    )
    try:
        text = result_import.decode_upload(data)
    except ValidationError as exc:
        preview.file_errors += exc.messages
        return preview
    if "\x00" in text:
        preview.file_errors.append("The file contains NUL characters; it is not a text CSV.")
        return preview
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
    except csv.Error as exc:
        preview.file_errors.append(f"Line 1: the header could not be read ({exc}).")
        return preview
    if header is None:
        preview.file_errors.append("The file is empty.")
        return preview
    names = _read_header(record_type, header, preview)
    if preview.file_errors:
        return preview
    present = set(names)
    seen = {}
    line = reader.line_num + 1
    try:
        for raw in reader:
            start, line = line, reader.line_num + 1
            if not any(cell.strip() for cell in raw):
                continue
            if len(preview.rows) >= MAX_ROWS:
                preview.file_errors.append(f"More than {MAX_ROWS} rows; split the file.")
                break
            row = Row(start, {})
            if len(raw) != len(names):
                row.errors.append(f"Expected {len(names)} cells, found {len(raw)}.")
            else:
                row.values = {name: cell.strip() for name, cell in zip(names, raw, strict=True)}
                _read_row(record_type, row, present, seen)
            preview.rows.append(row)
    except csv.Error as exc:
        preview.file_errors.append(f"Line {reader.line_num}: malformed CSV ({exc}).")
    if not preview.rows and not preview.file_errors:
        preview.file_errors.append("The file has a header but no data rows.")
    preview.state_token = _state_token(preview)
    return preview


def _state_token(preview):
    """Fingerprint of the file and every record it would change, to refuse a stale preview."""
    parts = [preview.record_type.slug, preview.file_hash]
    for row in preview.rows:
        existing = row.existing
        parts.append(
            f"{row.line}:{row.action}:{existing.pk if existing else ''}:"
            f"{record_token(existing) if existing else ''}"
        )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ------------------------------------------------------------------------- preview / commit


def _service_errors(record_type, exc):
    if hasattr(exc, "error_dict"):
        return [
            message if name == "__all__" else f"{record_type.column_for(name)}: {message}"
            for name, messages in exc.message_dict.items()
            for message in messages
        ]
    return list(exc.messages)


def _apply(record_type, actor, preview, reason):
    """Run every changing row through its service inside the caller's transaction."""
    source = f"{preview.filename}, SHA-256 {preview.file_hash}"
    for row in preview.rows:
        if row.errors or row.action not in ("create", "update"):
            continue
        if row.action == "create":
            row_reason = f"Created by import of {source}"
        else:
            row_reason = f"{reason or PREVIEW_REASON} (import of {source})"
        state = record_token(row.existing) if row.existing is not None else None
        try:
            with transaction.atomic():
                record_type.write(
                    record_type, actor, row.existing, row.new_fields, row.data, row_reason, state
                )
        except ValidationError as exc:
            row.errors += _service_errors(record_type, exc)
        except ObjectDoesNotExist:
            row.errors.append("This record no longer exists. Upload the file again.")
        except DatabaseError:  # the row's savepoint is rolled back; the file is then refused
            row.errors.append("The database refused this row. Check its values and try again.")
        except PermissionDenied:
            raise
        except Exception:  # never a 500 for one row: the file is refused and nothing is written
            logger.exception(
                "Setup import %s line %s failed unexpectedly", record_type.slug, row.line
            )
            row.errors.append(
                f"Line {row.line} could not be saved because of an unexpected error. "
                "Nothing was imported; check the row's values or report the problem."
            )


def preview_file(record_type, *, actor, data, filename):
    """What committing the file would do, row by row. Always rolled back: stores nothing."""
    require_role(actor, *record_type.roles)
    with transaction.atomic():
        preview = parse(record_type, data, filename)
        if not preview.file_errors:
            _apply(record_type, actor, preview, "")
        transaction.set_rollback(True)
    return preview


def commit_file(record_type, *, actor, data, filename, reason, state_token):
    """Check the file again and write every row, or nothing. Returns the preview it wrote.

    Raises ValidationError: {"reason": ...} when the file changes existing records and no reason
    was given; otherwise a message (stale preview, file or row errors). Nothing is written then."""
    require_role(actor, *record_type.roles)
    reason = (reason or "").strip()
    with transaction.atomic():
        preview = parse(record_type, data, filename)
        if not preview.ok:
            raise ValidationError(
                f"The file has {preview.error_count} error(s). Nothing was imported."
            )
        if preview.state_token != state_token:
            raise ValidationError(
                "The file or the records it changes are different from the preview. "
                "Check the new preview and import again."
            )
        if preview.needs_reason and not reason:
            raise ValidationError({"reason": "Give the reason for changing existing records."})
        _apply(record_type, actor, preview, reason)
        if not preview.ok:
            transaction.set_rollback(True)
            raise ValidationError(
                f"The file has {preview.error_count} error(s). Nothing was imported."
            )
        counts = preview.counts
        # No model owns an uploaded file: the summary event is keyed by the file's SHA-256.
        assert connection.in_atomic_block
        AuditEvent.objects.create(
            actor=actor,
            action=SUMMARY_ACTION,
            object_type="dataimport.file",
            object_id=preview.file_hash,
            after={
                "record_type": record_type.slug,
                "filename": preview.filename,
                "file_sha256": preview.file_hash,
                "file_bytes": preview.file_bytes,
                "rows": len(preview.rows),
                "created": counts["create"],
                "updated": counts["update"],
                "unchanged": counts["unchanged"],
            },
            reason=reason if preview.needs_reason else "",
        )
    return preview
