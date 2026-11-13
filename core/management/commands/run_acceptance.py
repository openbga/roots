"""Run the acceptance scripts in a scratch database and write an evidence package.

    python manage.py run_acceptance --operator "Name of the person running it"

The laboratory's working database is never written: see core/acceptance/scratch.py.

Exit status:
    0  every step passed and the package is complete;
    1  the package was written, but a step failed or did not run, or the package is incomplete
       (report.pdf could not be rendered) or the scratch database could not be cleaned up;
    2  refused or stopped before any step ran; the message says whether anything was created;
    3  unexpected failure; the message says what is missing.
"""

import logging
import os
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.acceptance import evidence, scratch
from core.acceptance.runner import PASSED, Run, load_steps, run_steps

ALL_PASSED, INCOMPLETE, REFUSED, UNEXPECTED = 0, 1, 2, 3
QUIET_LOGGERS = (
    "roots.request",
    "roots.signin",
    "django.request",
    "django.security",
    "weasyprint",
    "fontTools",
)


@contextmanager
def quiet_request_logs():
    """The run's own requests, refusals included, are recorded in the evidence; keep them out
    of the console. Errors still reach the log."""
    loggers = [logging.getLogger(name) for name in QUIET_LOGGERS]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def protected_roots():
    """Directories the application serves or stores uploads in; evidence never goes there."""
    roots = [settings.STATIC_ROOT, getattr(settings, "WHITENOISE_ROOT", None), settings.MEDIA_ROOT]
    roots += list(settings.STATICFILES_DIRS)
    return [Path(r).resolve() for r in roots if r]


def prepare_output(option, started):
    """Create the evidence directory and prove it is writable, before anything else happens."""
    stamp = started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path(option) if option else Path.cwd() / f"smart-roots-acceptance-{stamp}"
    path = path.resolve()
    for root in protected_roots():
        if path == root or root in path.parents:
            raise CommandError(
                f"Refusing to write evidence inside {root}, which the application serves. "
                "Nothing was changed.",
                returncode=REFUSED,
            )
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise CommandError(
            f"{path} already exists and is not empty; choose a new directory so earlier evidence "
            "is never overwritten. Nothing was changed.",
            returncode=REFUSED,
        )
    created = not path.exists()
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise CommandError(
            f"Cannot write the evidence package to {path} ({type(exc).__name__}). Nothing was "
            "changed.",
            returncode=REFUSED,
        ) from None
    return path, created


def remove_if_empty(path):
    """Leave no empty evidence directory behind when nothing was run."""
    try:
        path.rmdir()
    except OSError:
        pass


class Command(BaseCommand):
    help = (
        "Run the scripted acceptance checks on synthetic data in a separate scratch database and "
        "write an evidence package (report.html, report.pdf, evidence.json, artifacts and a "
        "SHA-256 manifest). The working database is never written. Exit status 0: every step "
        "passed; 1: package written, not every step passed or the package is incomplete; 2: "
        "refused before any step ran; 3: unexpected failure."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--operator",
            required=True,
            help="Name of the person running the checks, recorded as stated.",
        )
        parser.add_argument(
            "--output",
            help="New or empty directory for the evidence package (default: a timestamped "
            "directory in the current directory).",
        )
        parser.add_argument(
            "--scratch-database-url",
            help="Empty database to use instead of creating one, for a role that cannot create "
            "databases; the role must own its current schema. Prefer the "
            "ACCEPTANCE_DATABASE_URL environment variable, which keeps the password out of the "
            "process list.",
        )
        parser.add_argument(
            "--keep-scratch-database",
            action="store_true",
            help="Leave the scratch database in place after the run, for inspection.",
        )
        parser.add_argument(
            "--drop-stale-scratch-database",
            action="store_true",
            help="Remove a scratch database left by an earlier stopped or kept run first.",
        )

    def handle(self, *args, **options):
        operator = " ".join(options["operator"].split())
        if not operator:
            raise CommandError(
                "Give --operator: the name of the person running the checks.", returncode=REFUSED
            )
        started = timezone.localtime()
        out, created = prepare_output(options["output"], started)
        url = options["scratch_database_url"] or os.environ.get("ACCEPTANCE_DATABASE_URL") or None
        steps = load_steps()
        run = Run()
        say = self.stdout.write
        try:
            with scratch.scratch_database(
                url=url,
                keep=options["keep_scratch_database"],
                drop_stale=options["drop_stale_scratch_database"],
                say=say,
                secrets=(run.secrets, run.names),
            ) as database:
                say(f"Running {len(steps)} steps on synthetic records.")
                with quiet_request_logs():
                    results = run_steps(steps, run, progress=self._progress)
        except scratch.Refused as exc:
            if created:
                remove_if_empty(out)
            raise CommandError(str(exc), returncode=REFUSED) from None
        except Exception as exc:
            if created:
                remove_if_empty(out)
            raise CommandError(
                f"The run stopped unexpectedly ({type(exc).__name__}); no evidence package was "
                f"written to {out}. See the messages above for the scratch database.",
                returncode=UNEXPECTED,
            ) from exc
        except BaseException:  # Ctrl-C: the scratch database was removed; leave no empty folder
            if created:
                remove_if_empty(out)
            raise
        finished = timezone.localtime()
        try:
            record = evidence.build_evidence(
                results=results,
                started=started,
                finished=finished,
                operator=operator,
                database=database,
                artifacts=run.artifacts,
            )
            with quiet_request_logs():
                problems = evidence.write_package(
                    out, record, run.artifacts, run.secrets, run.names
                )
        except Exception as exc:
            raise CommandError(
                f"The steps ran but the evidence package could not be completed in {out} "
                f"({type(exc).__name__}). Any files there are incomplete and carry no manifest.",
                returncode=UNEXPECTED,
            ) from exc
        summary = record["summary"]
        say(
            f"{summary['passed']} passed, {summary['failed']} failed, {summary['not_run']} not run "
            f"of {summary['total']} steps in {record['duration_seconds']} s."
        )
        say(f"Evidence package: {out}")
        if not summary["all_passed"]:
            problems = ["Not every step passed; see report.html.", *problems]
        if problems:
            raise CommandError(" ".join(problems), returncode=INCOMPLETE)
        self.stdout.write(self.style.SUCCESS("Every step passed."))

    def _progress(self, result):
        style = self.style.SUCCESS if result.status == PASSED else self.style.ERROR
        line = f"  {result.step.id} {result.step.title}: {result.status}"
        self.stdout.write(style(line))
        if result.status != PASSED and result.observations:
            self.stdout.write(f"      {result.observations[-1]}")
