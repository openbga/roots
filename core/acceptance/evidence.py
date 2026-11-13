"""The evidence package: report.html, report.pdf, evidence.json, artifacts and a manifest.

Everything is written to a directory the operator chooses. The printable report is rendered
from an application template with escaped data and no external resources; the PDF renderer
refuses every network or file fetch, as report PDFs do. No secret, connection string, host
name or environment dump is written.
"""

import hashlib
import json
import os
import platform
import re
import subprocess
import tomllib
from pathlib import Path

import django
from django.conf import settings
from django.template.loader import render_to_string

from .runner import FAILED, LOOPBACK, NOT_RUN, PASSED, header_name, scrub_data

PRODUCT = "Smart Roots LIMS"
TEMPLATE = "acceptance/report.html"
MANIFEST = "manifest-sha256.txt"
COMMIT_VARIABLES = ("SOURCE_COMMIT", "RENDER_GIT_COMMIT", "GIT_COMMIT")


def software():
    """Name, version and, where the environment or a Git checkout supplies it, the commit."""
    version = tomllib.loads((settings.BASE_DIR / "pyproject.toml").read_text())["project"][
        "version"
    ]
    commit, source = None, None
    for variable in COMMIT_VARIABLES:
        value = os.environ.get(variable, "").strip()
        if re.fullmatch(r"[0-9a-f]{7,40}", value):
            commit, source = value, f"environment variable {variable}"
            break
    modified = None
    if commit is None:
        try:
            found = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=settings.BASE_DIR,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if found.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", found.stdout.strip()):
                commit, source = found.stdout.strip(), "Git checkout"
                status = subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=no"],
                    cwd=settings.BASE_DIR,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                modified = bool(status.stdout.strip()) if status.returncode == 0 else None
        except OSError, subprocess.SubprocessError:
            pass
    return {
        "name": PRODUCT,
        "version": version,
        "commit": commit,
        "commit_source": source or "not available in this environment",
        "tracked_files_modified": modified,
    }


def runtime():
    try:
        import weasyprint

        pdf = weasyprint.__version__
    except Exception:
        pdf = "not available"
    return {
        "python": platform.python_version(),
        "django": django.get_version(),
        "weasyprint": pdf,
    }


def settings_in_effect():
    return {
        "DEBUG": settings.DEBUG,
        "TIME_ZONE": settings.TIME_ZONE,
        "USE_TZ": settings.USE_TZ,
        "SECURE_SSL_REDIRECT": settings.SECURE_SSL_REDIRECT,
        "SECURE_HSTS_SECONDS": settings.SECURE_HSTS_SECONDS,
        "SESSION_COOKIE_SECURE": settings.SESSION_COOKIE_SECURE,
        "CSRF_COOKIE_SECURE": settings.CSRF_COOKIE_SECURE,
        "REPORT_SYNTHETIC_DATA_NOTICE": settings.REPORT_SYNTHETIC_DATA_NOTICE,
    }


def how_requests_were_made():
    """An exact account of the requests, from the settings in effect."""
    proxy = settings.SECURE_PROXY_SSL_HEADER
    parts = [
        "Requests were made in process with Django's request client against the application's "
        "own URL configuration, views, middleware and settings, without a network socket, so "
        "TLS, the proxy and the application server were not exercised."
    ]
    if proxy:
        header = header_name(proxy[0])
        parts.append(
            f"Each request carried the {header}: {proxy[1]} header that SECURE_PROXY_SSL_HEADER "
            "trusts, as the deployment's proxy adds it; one deliberate request without it showed "
            "how plain HTTP is answered."
        )
    else:
        parts.append("Each request was marked as HTTPS in the server environment.")
    client_header = getattr(settings, "ALLAUTH_TRUSTED_CLIENT_IP_HEADER", None)
    proxies = getattr(settings, "ALLAUTH_TRUSTED_PROXY_COUNT", 0)
    if client_header:
        parts.append(
            f"Each request carried the {client_header} client-address header the sign-in limits "
            f"trust, set to {LOOPBACK}."
        )
    elif proxies:
        parts.append(
            f"Each request carried an X-Forwarded-For header with {proxies} entr"
            f"{'y' if proxies == 1 else 'ies'} ({LOOPBACK}), as ALLAUTH_TRUSTED_PROXY_COUNT "
            "expects."
        )
    if "*" in settings.ALLOWED_HOSTS:
        parts.append("ALLOWED_HOSTS accepts any host name, so host validation was not exercised.")
    else:
        parts.append("Each request carried a host name from ALLOWED_HOSTS; host validation was on.")
    parts.append(
        "People signed in through the sign-in form with passwords generated for the run. The "
        "passwords are not written to this record; their hashes were stored in the scratch "
        "database like any account's and are removed with it unless it is kept. Form posts "
        "carried the CSRF token and a same-origin Origin header, and CSRF checking was enforced. "
        "Sign-in rate limits applied as configured; the run signs in fewer than fifteen times. "
        "Records were created through the application's forms and domain services; the "
        "history-protection checks wrote to history tables directly on purpose."
    )
    return " ".join(parts)


STATEMENT = (
    "This record shows that the scripted checks listed below were run on this installation of "
    "{product} at the times shown, in a separate scratch database holding synthetic records "
    "only, and whether each check passed. No record in the laboratory's working database was "
    "read or changed: the run read only that database's migration table and server identity, "
    "and, when it created the scratch database, used its connection to hold a lock and to "
    "create and drop the scratch database. It is not a statement that the software or the "
    "laboratory is validated, qualified, compliant, accredited or certified, and it does not "
    "test the laboratory's own methods, limits, data, users or procedures. The laboratory "
    "decides what this record means in its quality system."
)


def build_evidence(*, results, started, finished, operator, database, artifacts):
    counts = {
        key: sum(1 for r in results if r.status == status)
        for key, status in (("passed", PASSED), ("failed", FAILED), ("not_run", NOT_RUN))
    }
    problems = []
    cleanup = database.get("cleanup")
    if cleanup and cleanup not in ("dropped", "emptied", "kept for inspection"):
        problems.append(f"Scratch database cleanup {cleanup}.")
    return {
        "record": "Smart Roots LIMS acceptance run",
        "statement": STATEMENT.format(product=PRODUCT),
        "software": software(),
        "runtime": {**runtime(), "postgresql": database.get("postgresql")},
        "settings": settings_in_effect(),
        "operator": operator,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "database": {
            "mode": database.get("mode"),
            "description": {
                "created": "Scratch database created on the configured database server, migrated "
                "from empty, and dropped after the run",
                "supplied": "Scratch database supplied by the operator, emptied before use when "
                "it held an earlier acceptance run, migrated from empty, and emptied after the run",
            }.get(database.get("mode"), "Test database"),
            "scratch_name": database.get("name"),
            "after_run": cleanup or "not recorded",
            "kept_after_run": database.get("kept", False),
            "applied_migrations": database.get("applied_migrations", []),
            "working_database": (
                {
                    "pending_migrations": database["working_pending"],
                    "applied_but_unknown_to_this_release": database["working_unknown"],
                    "how_checked": "Read-only query of its migration table; nothing written",
                }
                if database.get("working_checked")
                else "migration state not readable (for example a database not yet migrated)"
            ),
        },
        "requests": {"how": how_requests_were_made()},
        "summary": {
            **counts,
            "total": len(results),
            "all_passed": counts["passed"] == len(results),
        },
        "problems": problems,
        "steps": [r.as_json() for r in results],
        "artifacts": [
            {
                "file": f"artifacts/{name}",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in sorted(artifacts.items())
        ],
    }


def migration_summary(applied):
    """Latest applied migration per application, for the printed report."""
    latest = {}
    for item in applied:
        app, name = item.split(".", 1)
        latest[app] = (latest.get(app, (0, ""))[0] + 1, max(name, latest.get(app, (0, ""))[1]))
    return [{"app": app, "count": n, "latest": name} for app, (n, name) in sorted(latest.items())]


def render_pdf(html):
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    class NoFetch(URLFetcher):
        def fetch(self, url, headers=None):
            raise ValueError("The acceptance report loads no external resources.")

    return HTML(string=html, base_url=None, url_fetcher=NoFetch()).write_pdf()


def _html(evidence):
    return render_to_string(
        TEMPLATE,
        {
            "e": evidence,
            "migrations": migration_summary(evidence["database"]["applied_migrations"]),
            "passed": PASSED,
            "failed": FAILED,
        },
    )


def write_package(directory, evidence, artifacts, secrets=(), names=()):
    """Write the package into `directory` (which exists and is writable). The evidence is
    scrubbed as data before anything is rendered. A report.pdf that cannot be rendered is
    recorded in `evidence["problems"]` and in report.html and evidence.json. Returns the
    problems list (empty for a complete package)."""
    directory = Path(directory)
    evidence = scrub_data(evidence, secrets, names)
    html = _html(evidence)
    try:
        pdf = render_pdf(html)
    except Exception as exc:
        pdf = None
        evidence["problems"].append(
            f"report.pdf could not be rendered ({type(exc).__name__}); report.html is complete."
        )
        html = _html(evidence)
    (directory / "artifacts").mkdir(exist_ok=True)
    for name, data in artifacts.items():
        (directory / "artifacts" / name).write_bytes(data)
    text = json.dumps(evidence, indent=2, ensure_ascii=False, default=str)
    (directory / "evidence.json").write_text(text + "\n", encoding="utf-8")
    (directory / "report.html").write_text(html, encoding="utf-8")
    if pdf is not None:
        (directory / "report.pdf").write_bytes(pdf)
    files = sorted(p for p in directory.rglob("*") if p.is_file() and p.name != MANIFEST)
    lines = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(directory).as_posix()}"
        for p in files
    ]
    (directory / MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return evidence["problems"]
