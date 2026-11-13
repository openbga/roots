"""Runs acceptance steps in order and records what each one observed.

A step is a plain function declared with `@step(...)`: identifier, title, requirement (with the
product document's outcome number where one applies), expected result and the steps it depends
on. It receives the `Run` and uses `run.check(...)` to compare what it observed with what was
expected; the first failed check ends that step. Every step runs in its own database savepoint,
so a failed step leaves nothing half-done for later steps. A failed step never stops later
independent steps; a step whose dependency did not pass is reported as not run.
"""

import re
import time
import traceback
from dataclasses import dataclass, field

import psycopg
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.test import Client

from .scratch import describe_db_error, long_enough

PASSED, FAILED, NOT_RUN = "passed", "failed", "not run"


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    requirement: str
    expected: str
    outcome: int | None
    depends: tuple[str, ...]
    function: object


@dataclass
class StepResult:
    step: Step
    status: str = NOT_RUN
    observations: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_json(self):
        return {
            "id": self.step.id,
            "title": self.step.title,
            "outcome": self.step.outcome,
            "requirement": self.step.requirement,
            "expected": self.step.expected,
            "depends_on": list(self.step.depends),
            "result": self.status,
            "observed": self.observations,
            "seconds": round(self.seconds, 2),
        }


REGISTRY = []


def step(id, title, *, requirement, expected, outcome=None, depends=()):
    """Declare an acceptance step. Steps run in the order they are declared."""

    def register(function):
        REGISTRY.append(Step(id, title, requirement, expected, outcome, tuple(depends), function))
        return function

    return register


def load_steps():
    """Every acceptance step, in run order: the journey, then the controls."""
    from . import controls, journey  # noqa: F401  (importing registers the steps)

    ids = [s.id for s in REGISTRY]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Acceptance step identifiers must be unique.")
    return list(REGISTRY)


class StepFailed(Exception):
    pass


class Run:
    """State shared by the steps of one run: named records, HTTP sessions and artifacts."""

    def __init__(self):
        self.records = {}
        self.artifacts = {}  # file name -> bytes, written to the evidence package
        self.secrets = []  # literal values kept out of the evidence: passwords
        self.names = []  # host and user names kept out of the evidence (as whole words)
        self._observations = None

    # ---------------------------------------------------------------- observations

    def observe(self, text):
        self._observations.append(scrub(text, self.secrets, self.names))

    def check(self, condition, observed, expected=None):
        """Record `observed`; fail the step when `condition` is false."""
        if condition:
            self.observe(observed)
            return
        message = f"{observed} (expected: {expected})" if expected else str(observed)
        raise StepFailed(scrub(message, self.secrets, self.names))

    def __getitem__(self, name):
        return self.records[name]

    def __setitem__(self, name, value):
        self.records[name] = value

    def artifact(self, name, data):
        self.artifacts[name] = bytes(data)

    def browser(self):
        return Browser(self)


def run_steps(steps, run=None, *, progress=None):
    """Run `steps` in order and return their results. Never raises for a failing step."""
    run = run or Run()
    results = {}
    for s in steps:
        result = StepResult(s)
        results[s.id] = result
        blocked = [d for d in s.depends if results.get(d) is None or results[d].status != PASSED]
        if blocked:
            result.observations.append(
                "Not run: it depends on "
                + ", ".join(
                    f"{d} ({results[d].status if d in results else 'unknown'})" for d in blocked
                )
                + "."
            )
        else:
            run._observations = result.observations
            # Records and sign-ins of a failed step are rolled back with it, so forget them too.
            kept = {k: dict(v) if isinstance(v, dict) else v for k, v in run.records.items()}
            started = time.monotonic()
            try:
                with transaction.atomic():
                    s.function(run)
                result.status = PASSED
            except StepFailed as exc:
                result.status = FAILED
                result.observations.append(f"Failed: {exc}")
                run.records = kept
            except Exception as exc:  # report any error as this step's failure, then go on
                result.status = FAILED
                result.observations.append(
                    "Failed with an unexpected error: "
                    + scrub(describe_error(exc), run.secrets, run.names)
                )
                run.records = kept
            finally:
                result.seconds = time.monotonic() - started
                run._observations = None
        if progress:
            progress(result)
    return list(results.values())


def describe_error(exc):
    """The error class, message and the acceptance-script line it came from; no locals. For a
    database error only the class and SQLSTATE: driver messages can name hosts and addresses."""
    where = ""
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        path = frame.filename.replace("\\", "/")
        if "/core/acceptance/" in path and not path.endswith("/runner.py"):
            where = f" at {frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}"
            break
    if isinstance(exc, DatabaseError | psycopg.Error):
        return describe_db_error(exc) + where
    message = " ".join(getattr(exc, "messages", None) or [str(exc)])
    return f"{type(exc).__name__}: {message[:500]}{where}"


WITHHELD = "[withheld]"
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}  # say nothing about a deployment
PATTERNS = [  # applied before literal replacement, so a password inside a URL cannot split it
    (re.compile(r"(?i)\bpostgres(?:ql)?(?:\+\w+)?://\S*"), "[connection string withheld]"),
    (re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/@]*@\S*"), "[connection string withheld]"),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|sslpassword)\s*=\s*('[^']*'|\"[^\"]*\"|\S+)"
        ),
        r"\1=" + WITHHELD,
    ),
    (
        re.compile(r"(?i)\b(host|hostaddr|user|dbname)\s*=\s*('[^']*'|\"[^\"]*\"|\S+)"),
        r"\1=" + WITHHELD,
    ),
]


def scrub(text, secrets=(), names=()):
    """Remove from evidence text: connection strings and keyword connection settings (by
    pattern, whatever their length); then the secret key and the run's generated passwords
    literally; then database passwords literally and database host and user names other than
    loopback as whole words, each only when it has at least `scratch.MIN_LITERAL` characters."""
    text = str(text)
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    db = connection.settings_dict
    literal = [settings.SECRET_KEY, *long_enough([db.get("PASSWORD")]), *secrets]
    for value in sorted({str(v) for v in literal if v}, key=len, reverse=True):
        text = text.replace(value, WITHHELD)
    names = {*long_enough([db.get("HOST"), db.get("USER")]), *long_enough(names)} - LOOPBACK_NAMES
    for value in names:
        text = re.sub(rf"(?<![\w.-]){re.escape(value)}(?![\w-])", WITHHELD, text)
    return text


def scrub_data(data, secrets=(), names=()):
    """`scrub` applied to every string in nested JSON-like data, before it is rendered."""
    if isinstance(data, dict):
        return {k: scrub_data(v, secrets, names) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [scrub_data(v, secrets, names) for v in data]
    return scrub(data, secrets, names) if isinstance(data, str) else data


# ------------------------------------------------------------------------------ HTTP

LOOPBACK = "127.0.0.1"
CSRF_PAGE = "The form could not be submitted"  # heading of the CSRF failure page (403.html)


def request_host():
    """A host name this deployment accepts (the first literal ALLOWED_HOSTS entry)."""
    for entry in settings.ALLOWED_HOSTS:
        entry = entry.strip()
        if entry and entry != "*" and not entry.startswith("."):
            return entry
    for entry in settings.ALLOWED_HOSTS:
        if entry.strip().startswith("."):
            return entry.strip()[1:]
    return "localhost"


def header_name(meta_key):
    """'HTTP_X_FORWARDED_PROTO' -> 'X-Forwarded-Proto'."""
    return "-".join(part.capitalize() for part in meta_key.removeprefix("HTTP_").split("_"))


def proxy_headers():
    """What the deployment's trusted proxy adds to each request: the HTTPS marker of
    SECURE_PROXY_SSL_HEADER, and the client address the sign-in limits trust (the configured
    client-address header, or X-Forwarded-For with ALLAUTH_TRUSTED_PROXY_COUNT entries)."""
    headers = {}
    if settings.SECURE_PROXY_SSL_HEADER:
        meta, value = settings.SECURE_PROXY_SSL_HEADER
        headers[header_name(meta)] = value
    if header := getattr(settings, "ALLAUTH_TRUSTED_CLIENT_IP_HEADER", None):
        headers[header] = LOOPBACK
    elif (count := getattr(settings, "ALLAUTH_TRUSTED_PROXY_COUNT", 0)) > 0:
        headers["X-Forwarded-For"] = ", ".join([LOOPBACK] * count)
    return headers


def role_refusal(response):
    """A 403 from the authorization checks, not from the CSRF check."""
    return response.status_code == 403 and CSRF_PAGE not in response.content.decode(
        "utf-8", "replace"
    )


def csrf_refusal(response):
    return response.status_code == 403 and CSRF_PAGE in response.content.decode("utf-8", "replace")


class Browser:
    """One person's session against the real URL configuration and middleware, in process.

    Each request carries what the deployment's proxy would add (see `proxy_headers`); where no
    SECURE_PROXY_SSL_HEADER is configured it is marked HTTPS directly. The Host is taken from
    ALLOWED_HOSTS, and form posts send the CSRF token and an Origin header as a browser does;
    CSRF checking is enforced. Errors come back as responses (status 500), never as exceptions.
    """

    def __init__(self, run):
        self.run = run
        self.host = request_host()
        self.client = Client(enforce_csrf_checks=True, raise_request_exception=False)
        self.username = None

    def _send(self, method, path, *, https=True, headers=None, **kwargs):
        sent = {"host": self.host, **proxy_headers(), **(headers or {})}
        proxied = bool(settings.SECURE_PROXY_SSL_HEADER)
        if not https and proxied:
            sent.pop(header_name(settings.SECURE_PROXY_SSL_HEADER[0]))
        return getattr(self.client, method)(
            path, secure=https and not proxied, headers=sent, **kwargs
        )

    def get(self, path, *, secure=True, headers=None):
        return self._send("get", path, https=secure, headers=headers)

    def post(self, path, data=None, *, csrf=True, origin=None, headers=None):
        data = dict(data or {})
        if csrf:
            token = self.client.cookies.get(settings.CSRF_COOKIE_NAME)
            if token is None:
                self.get("/accounts/login/")  # any form page issues the token cookie
                token = self.client.cookies.get(settings.CSRF_COOKIE_NAME)
            data.setdefault("csrfmiddlewaretoken", token.value if token else "")
        sent = {"origin": origin or f"https://{self.host}", **(headers or {})}
        return self._send("post", path, headers=sent, data=data)

    def sign_in(self, username, password):
        self.get("/accounts/login/")
        response = self.post("/accounts/login/", {"login": username, "password": password})
        self.username = username
        return response


def form_errors(response):
    """Field and form error text from a re-rendered form, for observations."""
    text = response.content.decode("utf-8", "replace")
    found = re.findall(
        r'class="[^"]*(?:invalid-feedback|errorlist|alert-danger)[^"]*"[^>]*>(.*?)<', text, re.S
    )
    return "; ".join(dict.fromkeys(" ".join(f.split()) for f in found if f.strip()))[:400]
