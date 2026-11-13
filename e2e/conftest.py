"""Browser suite fixtures: pytest-playwright + pytest-django live_server, headless Chromium.

Isolation: the suite uses its own PostgreSQL database (``test_<name>_e2e``, created and dropped
by pytest-django) and an ephemeral-port server thread. It never touches the configured
development/demo database, whatever DATABASE_URL points at.
"""

import os
import secrets
from dataclasses import dataclass

import pytest
from django.core.management import call_command
from helpers import sign_in
from playwright.sync_api import Page, expect

# Playwright's sync API keeps an asyncio loop running in the test thread; Django then refuses
# ORM calls there. Fixture/assertion queries are synchronous test-side code, so the guard is
# lifted for this suite only. The application server runs in its own thread and is unaffected.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

ACTION_TIMEOUT_MS = 10_000
EXPECT_TIMEOUT_MS = 5_000
NAVIGATION_TIMEOUT_MS = 15_000

DESKTOP = {"width": 1366, "height": 860}
TABLET = {"width": 820, "height": 1180}
NARROW = {"width": 390, "height": 844}


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    """Use a dedicated database name so this suite cannot collide with the unit-test database.

    With TEST_DATABASE_NAME set (test_<you>, as for the main suite), the browser suite uses
    <that name>_e2e, so people running it at the same time each get their own database; without
    it, test_<database>_e2e.
    """
    from django.conf import settings

    requested = os.environ.get("TEST_DATABASE_NAME", "").strip()
    if requested and not requested.startswith("test_"):
        raise RuntimeError("TEST_DATABASE_NAME must start with test_.")
    for config in settings.DATABASES.values():
        name = f"{requested}_e2e" if requested else f"test_{config['NAME']}_e2e"
        config.setdefault("TEST", {})["NAME"] = name
        assert config["TEST"]["NAME"] != config["NAME"]


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    # Unattended runs must never open a window, even if --headed is passed by habit.
    return {**browser_type_launch_args, "headless": True}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, live_server):
    port = int(live_server.url.rsplit(":", 1)[1])
    assert port != 8000, "E2E must not run against the shared development server port."
    return {
        **browser_context_args,
        "base_url": live_server.url,
        "viewport": DESKTOP,
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "color_scheme": "light",
        "reduced_motion": "reduce",
        "accept_downloads": True,
    }


@pytest.fixture(autouse=True)
def _bounded_timeouts(page: Page):
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    expect.set_options(timeout=EXPECT_TIMEOUT_MS)
    # Unexpected browser errors; error pages (403/404) legitimately log failed loads, so tests
    # assert on this list only for pages that should be clean.
    page.console_errors = errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    violations = []

    def record_policy_violation(message):
        text = str(message)
        if "content security policy" in text.lower() or "content-security-policy" in text.lower():
            violations.append(text)

    def watch_policy(target):
        target.on("console", lambda message: record_policy_violation(message.text))
        target.on("pageerror", record_policy_violation)

    watch_policy(page)
    page.context.on("page", watch_policy)
    yield
    assert not violations, "Content Security Policy violations:\n" + "\n".join(violations)


@pytest.fixture(autouse=True)
def _plain_static_storage(settings):
    """Serve current static source through staticfiles finders, not a possibly stale manifest.

    Production manifest storage is covered by the container smoke test.
    """
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture(autouse=True)
def _fresh_login_rate_limits():
    """Sign-in attempts are rate limited per client address in the process-wide cache. The
    journeys sign in dozens of times, so each test starts with an empty limiter, as a fresh
    server process would. The limiter itself stays enabled."""
    from django.core.cache import cache

    cache.clear()
    yield


@pytest.fixture(autouse=True)
def _isolation_guard(live_server, db_settings_name):
    assert db_settings_name.startswith("test_"), "E2E must run on a pytest-created test database."


@pytest.fixture
def db_settings_name():
    from django.db import connection

    return connection.settings_dict["NAME"]


@dataclass
class Lab:
    password: str


@pytest.fixture
def demo(transactional_db, monkeypatch):
    """Synthetic demo laboratory via the real seed commands, with a per-run random password."""
    password = secrets.token_urlsafe(18)
    monkeypatch.setenv("DEMO_PASSWORD", password)
    call_command("seed_demo", verbosity=0)
    call_command("seed_reporting_demo", verbosity=0)
    return Lab(password=password)


@pytest.fixture
def sign_in_as(page: Page, demo: Lab):
    def go(role: str):
        sign_in(page, f"demo_{role}", demo.password)
        return page

    return go


def pytest_addoption(parser):
    parser.addoption(
        "--screenshots-dir",
        default=None,
        help="Opt in to the milestone screenshot set and write the images to this directory.",
    )


def pytest_collection_modifyitems(config, items):
    """The screenshot journey produces review artifacts; it runs only when asked for."""
    if config.getoption("--screenshots-dir"):
        return
    chosen = [item for item in items if "screenshots" not in item.keywords]
    config.hook.pytest_deselected(items=[i for i in items if i not in chosen])
    items[:] = chosen
