# UI before/after comparison

Screenshots of pages before and after a change, for pull-request review. "Before" is
`origin/develop`, served from a temporary worktree beside the repository on 127.0.0.1:8001;
"after" is your running development server on 127.0.0.1:8000. Both read the same development
database, so they show the same synthetic demo records.

## Run

Start the development server with demo data (see CONTRIBUTING.md), then from the repository root:

```sh
set -a; . ./.env; set +a          # DATABASE_URL, DEMO_PASSWORD, …
uv run --group e2e python scripts/ui_compare/capture.py --list
uv run --group e2e python scripts/ui_compare/capture.py --pages samples,sample,demo_client:/portal/reports/
```

- `--pages`: ids from `--list`, a path such as `/orders/` (signed in as `demo_admin`), or
  `demo_client:/portal/…` for a client-portal page. Without it, every listed page is captured.
- `--after-only`: capture only the after side and reuse the existing before shots.

Each page is captured in light and dark, as the viewport and as a full page, with a pixel-difference
view and a horizontal-overflow measurement. Report PDFs are rasterised with Poppler (`pdftoppm`).

The output goes to the ignored `.local/ui-compare/`: open `index.html` there (the last line printed).
The command exits non-zero if a page could not be captured; `manifest.json` lists the gaps. Add
hand-written notes in `.local/ui-compare/notes.json` and rebuild the page with
`python3 scripts/ui_compare/build.py`.

The run stops before starting if the branch has migrations develop's code cannot share a database
with (anything but new tables, indexes, constraints or nullable/defaulted fields). The worktree and
the :8001 server are always removed at the end.
