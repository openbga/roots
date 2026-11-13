"""Light/dark/system theme: choice, persistence across pages, live system following, legibility."""

import pytest
from helpers import contrast_failures, sign_in, sign_out, theme_samples
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.django_db(transaction=True)

# Records which theme the root element carried when <body> first existed, i.e. before first
# paint of content and before any deferred script ran.
THEME_AT_BODY = """
new MutationObserver((_, observer) => {
  if (document.body) {
    window.__themeAtBody = document.documentElement.getAttribute('data-bs-theme');
    observer.disconnect();
  }
}).observe(document, {childList: true, subtree: true});
"""


def root_theme(page: Page) -> str:
    return page.evaluate("document.documentElement.getAttribute('data-bs-theme')")


def choose_theme(page: Page, label: str) -> None:
    page.get_by_role("button", name="Theme", exact=True).click()
    page.get_by_role("button", name=label, exact=True).click()


def stored_choice(page: Page):
    return page.evaluate("localStorage.getItem('vr-theme')")


@pytest.mark.browser_context_args(color_scheme="dark")
def test_theme_choice_persists_follows_system_and_stays_legible(page: Page, demo):
    page.add_init_script(THEME_AT_BODY)

    # No saved choice: the system preference (dark) applies before content paints.
    page.goto("/accounts/login/")
    assert page.evaluate("window.__themeAtBody") == "dark"
    assert stored_choice(page) is None

    # An explicit Light choice beats the dark system setting and survives sign-in and reload.
    choose_theme(page, "Light")
    assert root_theme(page) == "light"
    assert stored_choice(page) == "light"
    sign_in(page, "demo_custodian", demo.password, visit=False)
    expect(page.get_by_role("heading", level=1, name="Dashboard")).to_be_visible()
    assert page.evaluate("window.__themeAtBody") == "light"
    light = theme_samples(page)
    page.reload()
    assert page.evaluate("window.__themeAtBody") == "light"

    # Dark, chosen explicitly while the system is light, is also kept across pages.
    page.emulate_media(color_scheme="light")
    choose_theme(page, "Dark")
    assert root_theme(page) == "dark"
    page.get_by_role("link", name="Samples", exact=True).first.click()
    expect(page.get_by_role("heading", level=1, name="Samples")).to_be_visible()
    assert page.evaluate("window.__themeAtBody") == "dark"
    page.get_by_role("link", name="Dashboard", exact=True).click()
    dark = theme_samples(page)

    # Printing from dark mode uses the light print palette, without a background image.
    page.emulate_media(media="print")
    printed = theme_samples(page)
    assert page.evaluate("getComputedStyle(document.body).backgroundImage") == "none"
    page.emulate_media(media="screen")

    # System follows the browser preference live, without reloading.
    choose_theme(page, "System")
    assert stored_choice(page) == "system"
    assert root_theme(page) == "light"
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-bs-theme", "dark")
    page.get_by_role("button", name="Theme", exact=True).click()
    expect(page.get_by_role("button", name="System", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.keyboard.press("Escape")

    # Legibility in both themes: WCAG AA text contrast (4.5:1) and non-text contrast (3:1).
    assert light["scheme"] == "light" and dark["scheme"] == "dark"
    assert light["pairs"]["body text"]["candidates"] != dark["pairs"]["body text"]["candidates"]
    failures = contrast_failures(light) + contrast_failures(dark) + contrast_failures(printed)
    # Print uses the darkest Evergreen ink on white (docs/ui-standards.md section 12).
    assert printed["pairs"]["body text"]["candidates"] == [[[23, 28, 22], [255, 255, 255]]]

    # List controls (filters) in both themes.
    page.get_by_role("link", name="Samples", exact=True).first.click()
    failures += contrast_failures(theme_samples(page))
    choose_theme(page, "Light")
    failures += contrast_failures(theme_samples(page))

    # Keyboard focus is visibly indicated in the current (dark) theme.
    page.keyboard.press("Tab")
    focus = page.evaluate(
        """() => { const el = document.activeElement, s = getComputedStyle(el);
        return {tag: el.tagName, outline: s.outlineStyle, width: parseFloat(s.outlineWidth),
                shadow: s.boxShadow}; }"""
    )
    assert focus["outline"] != "none" and focus["width"] >= 2 or focus["shadow"] != "none", focus
    assert page.console_errors == []
    assert not failures, "\n".join(dict.fromkeys(failures))
    sign_out(page)
