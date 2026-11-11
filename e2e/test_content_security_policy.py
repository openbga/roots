"""Exercise script surfaces outside the workflow journeys under the enforced policy."""

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.urls import reverse
from helpers import released_water_report, sign_in, sign_out
from playwright.sync_api import expect

from accounts.models import User
from commercial.models import Quote
from core.models import Lab

pytestmark = pytest.mark.django_db(transaction=True)


def assert_policy(response):
    assert response.status == 200
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert "camera=()" in response.headers["permissions-policy"]


def test_admin_and_identity_widgets_use_local_scripts(page, demo):
    admin = User.objects.get(username="demo_admin")
    admin.is_staff = admin.is_superuser = True
    admin.save(update_fields=["is_staff", "is_superuser"])
    sign_in(page, admin.username, demo.password)
    assert page.evaluate("htmx.config.allowEval") is False
    assert page.evaluate("htmx.config.includeIndicatorStyles") is False
    routes = [
        reverse("admin:index"),
        reverse("admin:accounts_user_changelist"),
        reverse("admin:accounts_user_add"),
        reverse("admin:accounts_user_change", args=[admin.pk]),
        reverse("admin:auth_group_changelist"),
        reverse("admin:auth_group_change", args=[Group.objects.get(name="analyst").pk]),
        reverse("admin:core_lab_change", args=[Lab.objects.get().pk]),
        reverse("account_email"),
        reverse("mfa_index"),
        reverse("mfa_activate_totp"),
    ]
    for route in routes:
        assert_policy(page.goto(route))
        expect(page.locator("body")).to_be_visible()
        # Admin filter_horizontal widgets are initialized by the external SelectFilter script.
        if route == reverse("admin:auth_group_change", args=[Group.objects.get(name="analyst").pk]):
            expect(page.locator("#id_permissions_from")).to_be_visible()
            page.locator("#id_permissions_input").fill("release")
        assert page.console_errors == [], f"{route}: {page.console_errors}"


def test_pdf_preview_is_embeddable_and_issued_download_keeps_stored_bytes(page, demo):
    report, version, _sample, _revision = released_water_report(
        "SYNTH-CSP", title="Synthetic policy report"
    )
    sign_in(page, "demo_approver", demo.password)
    with page.expect_response(lambda response: "/preview.pdf" in response.url) as embedded:
        assert_policy(page.goto(reverse("reporting:report-preview", args=[report.pk])))
        page.locator("iframe").scroll_into_view_if_needed()
    preview = embedded.value
    assert preview.status == 200
    assert "frame-ancestors 'self'" in preview.headers["content-security-policy"]
    assert preview.headers["content-type"] == "application/pdf"
    expect(page.locator("iframe")).to_be_visible()
    response = page.request.get(reverse("reporting:version-pdf", args=[report.pk, version.number]))
    assert_policy(response)
    assert response.body() == bytes(version.file.pdf)
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert page.console_errors == []


def test_staff_and_client_quote_print_buttons_use_external_handler(page, demo):
    call_command("seed_commercial_demo", verbosity=0)
    client = User.objects.get(username="demo_client")
    quote = Quote.objects.filter(client_id=client.client_id, status=Quote.Status.ISSUED).first()
    assert quote is not None
    for username, namespace in [
        ("demo_project_manager", "commercial"),
        (client.username, "client_commercial"),
    ]:
        sign_in(page, username, demo.password)
        assert_policy(page.goto(reverse(f"{namespace}:quote-detail", args=[quote.pk])))
        page.evaluate("window.print = () => { window.syntheticPrintCalled = true; }")
        page.get_by_role("button", name="Print", exact=True).click()
        assert page.evaluate("window.syntheticPrintCalled === true")
        if namespace == "commercial":
            sign_out(page)


@pytest.mark.parametrize("status", [409, 503])
def test_preview_refusal_is_readable_inside_the_frame(page, demo, monkeypatch, status):
    from reporting.models import Report
    from reporting.pdf import ReportRenderError

    report = Report.objects.first()
    sign_in(page, "demo_approver", demo.password)
    if status == 409:
        message = "The report content changed. Reload the preview page."
        page.route(
            "**/preview.pdf?*",
            lambda route: route.continue_(url=route.request.url.split("?")[0] + "?fp=stale"),
        )
    else:
        message = "Synthetic preview rendering is unavailable."

        def render_failure(*args, **kwargs):
            raise ReportRenderError(message)

        monkeypatch.setattr("reporting.views.render_pdf", render_failure)

    with page.expect_response(lambda response: "/preview.pdf" in response.url) as embedded:
        assert_policy(page.goto(reverse("reporting:report-preview", args=[report.pk])))
        page.locator("iframe").scroll_into_view_if_needed()
    response = embedded.value
    assert response.status == status
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    expect(page.frame_locator("iframe").get_by_text(message, exact=True)).to_be_visible()


def test_csrf_failure_page_has_no_inline_policy_violation(page, demo):
    page.goto("/accounts/login/")
    page.locator('[name="csrfmiddlewaretoken"]').evaluate("element => element.remove()")
    with page.expect_response(
        lambda response: response.request.method == "POST" and "/accounts/login/" in response.url
    ) as refused:
        sign_in(page, "demo_approver", demo.password, visit=False)
    assert refused.value.status == 403
    expect(page.get_by_role("heading", name="The form could not be submitted")).to_be_visible()
    expect(page.get_by_text("This form could not be verified.", exact=False)).to_be_visible()
    assert page.locator("style").count() == 0
