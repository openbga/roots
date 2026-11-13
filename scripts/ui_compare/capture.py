"""Before/after screenshots of the UI for a pull request.

"Before" is origin/develop, served from a temporary worktree on 127.0.0.1:8001; "after" is the
running development server on 127.0.0.1:8000. Both read the same development database, so every
page shows the same synthetic records. See README.md in this directory.

    uv run --group e2e python scripts/ui_compare/capture.py [--pages ID_OR_PATH,...] [--after-only]
    uv run --group e2e python scripts/ui_compare/capture.py --list
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = REPO / ".local" / "ui-compare"
SHOTS = OUT / "shots"
BEFORE_TREE = REPO.parent / f"{REPO.name}-before"
AFTER = "http://127.0.0.1:8000"
BEFORE = "http://127.0.0.1:8001"
VIEWPORT = {"width": 1440, "height": 900}
LAPTOP = {"width": 1280, "height": 800}
THEMES = ("light", "dark")
STAFF, CLIENT = "demo_admin", "demo_client"  # synthetic demo accounts only


# ------------------------------------------------------------------------------- page actions


def settle(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(400)


def new_client_error(page: Page) -> None:
    """Open the New client dialog and submit it without a name: one inline error, nothing saved."""
    page.get_by_role("link", name="New client").first.click()
    dialog = page.locator(".modal.show").first
    dialog.wait_for(state="visible")
    settle(page)
    dialog.locator("[name=name]").first.fill("")
    dialog.locator("[name=account_number]").first.fill("PREVIEW-0")
    dialog.locator("button[type=submit].btn-primary").first.click()
    page.get_by_text("This field is required").first.wait_for()
    settle(page)


def open_select(page: Page) -> None:
    control = page.locator("#id_project-ts-control")
    if control.count():
        if not page.evaluate("document.getElementById('id_project').tomselect.isOpen"):
            control.click()
        page.locator(".ts-dropdown:visible").first.wait_for()
    page.wait_for_timeout(300)


def open_datetime(page: Page) -> None:
    icon = page.locator("#id_received_at + .vr-date-icon")
    (icon if icon.count() else page.locator("#id_received_at")).first.click()
    page.wait_for_timeout(400)


# ------------------------------------------------------------------------------- page registry

# id: (title, account, path or "@record" resolved from a list, action, viewport)
PAGES = {
    "dashboard": ("Dashboard", STAFF, "/", None, VIEWPORT),
    "samples": ("Samples list", STAFF, "/samples/", None, VIEWPORT),
    "sample": ("Sample record", STAFF, "@sample", None, VIEWPORT),
    "new-client": (
        "New client dialog with an error",
        STAFF,
        "/clients/",
        new_client_error,
        VIEWPORT,
    ),
    "client": ("Client record", STAFF, "@client", None, VIEWPORT),
    "batch": ("Batch detail", STAFF, "@batch", None, VIEWPORT),
    "review": ("Technical review of a batch", STAFF, "@review", None, VIEWPORT),
    "worklist": ("Worklist", STAFF, "/analytical/worklist/", None, VIEWPORT),
    "equipment": ("Equipment record", STAFF, "@equipment", None, VIEWPORT),
    "tests": ("Catalog tests list", STAFF, "/catalog/", None, VIEWPORT),
    "report": ("Report preview", STAFF, "@report", None, VIEWPORT),
    "quote": ("Quote", STAFF, "@quote", None, VIEWPORT),
    "receipt-select": ("Receipt form, select open", STAFF, "/samples/new/", open_select, VIEWPORT),
    "receipt-date": (
        "Receipt form, date-time open",
        STAFF,
        "/samples/new/",
        open_datetime,
        VIEWPORT,
    ),
    "portal": ("Client portal home", CLIENT, "/portal/", None, VIEWPORT),
}
for _path, _title in (
    ("/samples/", "Samples list"),
    ("/clients/", "Clients list"),
    ("/analytical/worklist/", "Worklist"),
    ("/analytical/batches/", "Batches & QC list"),
    ("/analytical/preparation/", "Preparation batches list"),
    ("/analytical/review/", "Technical review list"),
    ("/catalog/", "Catalog tests list"),
    ("/reports/", "Reports list"),
    ("/commercial/quotes/", "Quotes list"),
    ("/laboratory/equipment/", "Equipment list"),
):
    PAGES[_path.strip("/").split("/")[-1] + "-1280"] = (
        f"{_title} at 1280",
        STAFF,
        _path,
        None,
        LAPTOP,
    )
PDFS = {  # live preview PDFs, every page rasterised (no colour theme)
    "pdf-original": "Report PDF preview, original release",
    "pdf-amendment": "Report PDF preview, amendment",
}
RECORDS = {  # "@record": (list page, link pattern)
    "sample": ("/samples/", r"/samples/[0-9a-f-]{36}/"),
    "client": ("/clients/", r"/clients/\d+/"),
    "batch": ("/analytical/batches/", r"/analytical/batches/\d+/"),
    "review": ("/analytical/review/", r"/analytical/review/batches/\d+/"),
    "equipment": ("/laboratory/equipment/", r"/laboratory/equipment/\d+/"),
    "report": ("/reports/", r"/reports/\d+/"),
    "quote": ("/commercial/quotes/", r"/commercial/quotes/\d+/"),
}

OVERFLOW_JS = """() => {
  const out = [];
  if (document.documentElement.scrollWidth > innerWidth + 1)
    out.push(`page ${document.documentElement.scrollWidth}px > ${innerWidth}px`);
  document.querySelectorAll('table').forEach(t => {
    let box = t.parentElement;
    while (box && box !== document.body && getComputedStyle(box).overflowX === 'visible')
      box = box.parentElement;
    if (box && box !== document.body && t.offsetParent && box.scrollWidth > box.clientWidth + 1)
      out.push(`table ${box.scrollWidth}px in ${box.clientWidth}px box`);
  });
  return out;
}"""


def selected_pages(spec: str) -> dict:
    """Registry ids, or ad-hoc paths: "/path/" (staff) or "demo_client:/path/"."""
    if not spec:
        return {
            **PAGES,
            **{pid: (title, STAFF, "@" + pid, "pdf", VIEWPORT) for pid, title in PDFS.items()},
        }
    pages = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        account, _, path = item.rpartition(":") if ":/" in item else ("", "", item)
        if path.startswith("/"):
            account = account or STAFF
            if account not in (STAFF, CLIENT):
                sys.exit(f"Unknown account {account!r}: use {STAFF} (default) or {CLIENT}.")
            pid = re.sub(r"[^a-z0-9]+", "-", f"{account}{path}".lower()).strip("-")
            pages[pid] = (f"{path} ({account})", account, path, None, VIEWPORT)
        elif item in PAGES:
            pages[item] = PAGES[item]
        elif item in PDFS:
            pages[item] = (PDFS[item], STAFF, "@" + item, "pdf", VIEWPORT)
        else:
            sys.exit(f"Unknown page {item!r}. Run with --list to see the ids.")
    return pages


# ------------------------------------------------------------------------------- environment


def run(cmd: list[str], cwd: Path = REPO) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout


def wait_http(url: str, seconds: int = 120) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=3)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"{url} did not answer within {seconds}s")


def goto(page: Page, url: str) -> None:
    """Navigate, riding out a development-server autoreload (connection refused for a moment)."""
    for attempt in range(3):
        try:
            page.goto(url)
            return
        except Exception as exc:
            if "ERR_CONNECTION_REFUSED" not in str(exc) or attempt == 2:
                raise
            wait_http(url.split("/", 3)[0] + "//" + url.split("/", 3)[2] + "/accounts/login/")


ADDITIVE = {"CreateModel", "AddIndex", "AddConstraint"}


def migration_problems() -> list[str]:
    """Migrations develop's code cannot share a database with.

    Allowed: new migration files whose operations are all CreateModel, AddIndex or AddConstraint,
    or AddField with null=True, a default or a db_default. Edited or deleted develop migrations
    are not.
    """
    problems, new_files = [], []
    edited = run(["git", "diff", "origin/develop", "--name-status", "--", "*/migrations/*"])
    for line in filter(None, edited.splitlines()):
        status, path = line.split("\t", 1)
        if status.startswith("A"):
            new_files.append(path)
        else:
            problems.append(f"{path}: {status} changes a develop migration")
    new_files += run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*/migrations/*"]
    ).split()
    for path in new_files:
        if not path.endswith(".py") or path.endswith("__init__.py"):
            continue
        for op in re.split(r"(?=migrations\.[A-Z]\w+\()", (REPO / path).read_text())[1:]:
            name = re.match(r"migrations\.(\w+)", op).group(1)
            additive_field = name == "AddField" and re.search(r"null=True|default=|db_default=", op)
            if name not in ADDITIVE and not additive_field:
                problems.append(f"{path}: {name} is not additive")
    return problems


def preflight() -> None:
    if not os.environ.get("DEMO_PASSWORD"):
        sys.exit("DEMO_PASSWORD is not set. Load the environment first: set -a; . ./.env; set +a")
    problems = migration_problems()
    if problems:
        sys.exit("Before and after cannot share the database:\n  " + "\n  ".join(problems))
    try:
        urllib.request.urlopen(AFTER + "/accounts/login/", timeout=5)
    except Exception:
        sys.exit(f"No development server answers on {AFTER}.")


def remove_worktree() -> None:
    if BEFORE_TREE.exists() or str(BEFORE_TREE) in run(["git", "worktree", "list"]):
        subprocess.run(["git", "worktree", "remove", "--force", str(BEFORE_TREE)], cwd=REPO)
        subprocess.run(["git", "worktree", "prune"], cwd=REPO)
    shutil.rmtree(BEFORE_TREE, ignore_errors=True)


def start_before() -> subprocess.Popen:
    remove_worktree()
    run(["git", "worktree", "add", "--detach", str(BEFORE_TREE), "origin/develop"])
    env = {
        k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")
    }
    for cmd in (
        ["uv", "sync", "--frozen"],
        ["uv", "run", "python", "manage.py", "collectstatic", "--noinput"],
    ):
        subprocess.run(cmd, cwd=BEFORE_TREE, env=env, check=True, capture_output=True)
    log = open(OUT / "before-server.log", "w")  # noqa: SIM115 (lives as long as the server)
    proc = subprocess.Popen(
        ["uv", "run", "python", "manage.py", "runserver", BEFORE.split("//")[1], "--noreload"],
        cwd=BEFORE_TREE,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    wait_http(BEFORE + "/accounts/login/")
    return proc


def stop_before(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    remove_worktree()


# ------------------------------------------------------------------------------- capture


def login(browser, base: str, account: str, theme: str):
    ctx = browser.new_context(viewport=VIEWPORT, color_scheme=theme, reduced_motion="reduce")
    ctx.add_init_script(f"try {{ localStorage.setItem('vr-theme', '{theme}'); }} catch (e) {{}}")
    page = ctx.new_page()
    goto(page, base + "/accounts/login/")
    page.fill("input[name=login]", account)
    page.fill("input[name=password]", os.environ["DEMO_PASSWORD"])
    page.locator("form button[type=submit]").first.click()
    page.wait_for_load_state("networkidle")
    if "/accounts/login" in page.url:
        raise RuntimeError(f"Sign-in failed for {account} on {base}")
    return ctx, page


def resolve_records(page: Page, wanted: set[str]) -> dict[str, str]:
    """Record paths, found once on the after server and reused for both."""
    urls = {}
    for key in wanted & set(RECORDS):
        list_path, pattern = RECORDS[key]
        goto(page, AFTER + list_path)
        settle(page)
        hrefs = page.eval_on_selector_all(
            "main a[href]", "els => els.map(e => e.getAttribute('href'))"
        )
        found = next(
            (h.split("?")[0] for h in hrefs if re.fullmatch(pattern, h.split("?")[0])), None
        )
        if found:
            urls[key] = found + ("preview/" if key == "report" else "")
    if wanted & set(PDFS):  # one draft original and one amendment, told apart by the preview title
        goto(page, AFTER + "/reports/")
        hrefs = page.eval_on_selector_all(
            "main a[href]", "els => els.map(e => e.getAttribute('href'))"
        )
        for href in dict.fromkeys(h for h in hrefs if re.fullmatch(r"/reports/\d+/", h)):
            goto(page, AFTER + href + "preview/")
            title = page.locator("h1").first.inner_text().lower()
            urls.setdefault(
                "pdf-amendment" if "amendment" in title else "pdf-original", href + "preview.pdf"
            )
    return urls


def capture(browser, side: str, base: str, pages: dict, urls: dict, result: dict) -> None:
    for theme in THEMES:
        sessions = {}
        try:
            for pid, (_title, account, target, action, viewport) in pages.items():
                if action == "pdf":
                    continue
                stem = SHOTS / f"{pid}-{side}-{theme}"
                try:
                    if account not in sessions:
                        sessions[account] = login(browser, base, account, theme)
                    page = sessions[account][1]
                    path = urls[target[1:]] if target.startswith("@") else target
                    page.set_viewport_size(viewport)
                    goto(page, base + path)
                    settle(page)
                    if action:
                        action(page)
                    result["overflow"][f"{pid}-{side}-{theme}"] = page.evaluate(OVERFLOW_JS)
                    shot = {"animations": "disabled", "caret": "hide"}
                    page.screenshot(path=f"{stem}.png", **shot)
                    page.screenshot(path=f"{stem}-full.png", full_page=True, **shot)
                    print(f"  {side:6} {theme:5} {pid}", flush=True)
                except Exception as exc:  # keep going; the page lists the gap
                    result["errors"].append(
                        f"{side} {theme} {pid}: {type(exc).__name__}: {exc}"[:300]
                    )
                    print("  !", result["errors"][-1], flush=True)
                    for stale in (Path(f"{stem}.png"), Path(f"{stem}-full.png")):
                        stale.unlink(missing_ok=True)  # never show an old shot as current
        finally:
            for ctx, _ in sessions.values():
                ctx.close()


def capture_pdfs(browser, side: str, base: str, pages: dict, urls: dict, result: dict) -> None:
    """Rasterise each live preview PDF with Poppler: page 1 as the viewport, all pages as full."""
    from PIL import Image

    wanted = [pid for pid, page in pages.items() if page[3] == "pdf"]
    if not wanted:
        return
    ctx, _ = login(browser, base, STAFF, "light")
    try:
        for pid in wanted:
            stem = SHOTS / f"{pid}-{side}"
            try:
                if pid not in urls:
                    raise RuntimeError("no matching report in the demo data")
                response = ctx.request.get(base + urls[pid], timeout=120000)
                if not response.ok or "pdf" not in response.headers.get("content-type", ""):
                    raise RuntimeError(f"HTTP {response.status}")
                pdf = SHOTS / f"{pid}-{side}.pdf"
                pdf.write_bytes(response.body())
                for old in SHOTS.glob(f"{pid}-{side}-p*.png"):
                    old.unlink()
                subprocess.run(["pdftoppm", "-r", "110", "-png", str(pdf), f"{stem}-p"], check=True)
                sheets = [
                    Image.open(f).convert("RGB") for f in sorted(SHOTS.glob(f"{pid}-{side}-p*.png"))
                ]
                width, gap = max(s.width for s in sheets), 24
                full = Image.new(
                    "RGB", (width, sum(s.height + gap for s in sheets)), (120, 120, 120)
                )
                y = 0
                for sheet in sheets:
                    full.paste(sheet, (0, y))
                    y += sheet.height + gap
                for theme in THEMES:
                    sheets[0].save(f"{stem}-{theme}.png")
                    full.save(f"{stem}-{theme}-full.png")
                print(f"  {side:6} pdf   {pid} ({len(sheets)} pages)", flush=True)
            except Exception as exc:
                result["errors"].append(f"{side} {pid}: {type(exc).__name__}: {exc}"[:300])
                print("  !", result["errors"][-1], flush=True)
                for theme in THEMES:
                    for stale in (Path(f"{stem}-{theme}.png"), Path(f"{stem}-{theme}-full.png")):
                        stale.unlink(missing_ok=True)
    finally:
        ctx.close()


def diff_stats(pids) -> dict:
    """Share of changed pixels per page and theme, plus a highlighted diff image."""
    from PIL import Image, ImageChops

    stats = {}
    for pid in pids:
        for theme in THEMES:
            a, b = SHOTS / f"{pid}-before-{theme}.png", SHOTS / f"{pid}-after-{theme}.png"
            if not (a.exists() and b.exists()):
                continue
            before, after = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
            if before.size != after.size:
                after = after.resize(before.size)
            mask = (
                ImageChops.difference(before, after)
                .convert("L")
                .point(lambda v: 255 if v > 10 else 0)
            )
            overlay = Image.blend(after, Image.new("RGB", after.size, (255, 255, 255)), 0.7)
            overlay.paste(Image.new("RGB", after.size, (230, 40, 90)), mask=mask)
            overlay.save(SHOTS / f"{pid}-diff-{theme}.png")
            stats[f"{pid}-{theme}"] = round(
                100 * mask.histogram()[255] / (mask.width * mask.height), 1
            )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--pages", default="", help="ids from --list, '/path/' or 'demo_client:/path/'"
    )
    parser.add_argument("--after-only", action="store_true", help="reuse the existing before shots")
    parser.add_argument("--list", action="store_true", help="print the page ids and exit")
    args = parser.parse_args()
    if args.list:
        for pid, page in PAGES.items():
            print(f"{pid:18} {page[0]}  [{page[2]}]")
        for pid, title in PDFS.items():
            print(f"{pid:18} {title}")
        return 0

    pages = selected_pages(args.pages)
    preflight()
    SHOTS.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "manifest.json"
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    result = {"errors": [], "overflow": {}}
    proc = None
    try:
        if not args.after_only:
            print(f"Starting origin/develop on {BEFORE} …", flush=True)
            proc = start_before()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(ignore_default_args=["--hide-scrollbars"])
            ctx, page = login(browser, AFTER, STAFF, "light")
            wanted = {t[1:] for _, _, t, _, _ in pages.values() if t.startswith("@")}
            urls = resolve_records(page, wanted)
            ctx.close()
            for side, base in [("after", AFTER)] + (
                [] if args.after_only else [("before", BEFORE)]
            ):
                capture(browser, side, base, pages, urls, result)
                capture_pdfs(browser, side, base, pages, urls, result)
            browser.close()
    finally:
        stop_before(proc)

    known = {p["id"]: p for p in old.get("pages", [])}
    for pid, (title, account, target, _action, viewport) in pages.items():
        path = urls.get(target[1:], "") if target.startswith("@") else target
        known[pid] = {
            "id": pid,
            "title": title,
            "user": account,
            "path": path,
            "viewport": f"{viewport['width']}×{viewport['height']}",
        }
    manifest = {
        "captured": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "before_ref": run(["git", "rev-parse", "--short", "origin/develop"]).strip(),
        "after_ref": run(["git", "rev-parse", "--short", "HEAD"]).strip() + " + working tree",
        "pages": list(known.values()),
        "errors": result["errors"],
        "overflow": {**old.get("overflow", {}), **result["overflow"]},
        "diff": {**old.get("diff", {}), **diff_stats(pages)},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    subprocess.run([sys.executable, str(HERE / "build.py"), str(OUT)], check=True)
    if result["errors"]:
        print("Capture gaps:\n  " + "\n  ".join(result["errors"]))
    print(OUT / "index.html")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
