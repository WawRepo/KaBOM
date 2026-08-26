# KaBOM

*"Your SBOMs, without the enterprise."*

KaBOM is a small web app that reads CycloneDX SBOMs (Software Bill of
Materials) out of MinIO (an S3-compatible object store) and lets one person
search them. It is built for a 12-node Raspberry Pi homelab with 27 SBOM
files, not for a company with thousands. It does not scan anything — Grype
does that separately, and KaBOM only ever reads what that job produces.

## Development

```bash
uv sync                  # install dependencies
uv run pytest            # run tests
uv run ruff check .      # lint
uv run ruff format .     # format
uv run uvicorn kabom.main:app --reload    # dev server
```

The dev server needs `KABOM_S3_ENDPOINT`, `KABOM_S3_BUCKET`,
`KABOM_S3_ACCESS_KEY`, `KABOM_S3_SECRET_KEY` set to point at a MinIO — see
"Try it locally with docker compose" below for the fastest way to get all of
that (including sample data) with one command.

## The UI

Three screens, server-rendered with Jinja2 + HTMX, styled with Tailwind CSS
(compiled by the standalone CLI, not npm):

- `/` — search, grouped by package name, results stream in as you type.
- `/sboms` — every ingested SBOM as a card, sorted **oldest first**.
- `/sboms/{id}` — one SBOM's full component list, filterable in the browser.

Every page also renders the **freshness banner** — see CLAUDE.md's "the thing
KaBOM must never do." It reads `GET /api/status` and uses the **oldest**
SBOM's age:

| Level | When | Looks like |
|---|---|---|
| GREEN | oldest SBOM < 24 hours old, nothing failed to parse | "Updated 20 minutes ago · 27 of 27 read" |
| AMBER | oldest SBOM between 24 hours and 7 days old | "Updated 2 days ago · 27 of 27 read" |
| RED | oldest SBOM ≥ 7 days old, **or** its age is unknown, **or** any file failed to parse this run | "STALE — oldest data is 39 days old · 25 of 27 read" + "These answers may be wrong. Check the SBOM job." |

A failed parse forces RED on its own, regardless of age — see
`kabom.main._freshness_banner`. When the banner is RED, the search results on
`/` also get a visible red border (`kabom/templates/_search_results.html`) —
the warning sits next to the answer, not only at the top of the page.

**Progressive enhancement, not a JS-only widget.** `/`'s search box is a
plain `<form method="get" action="/">`. With HTMX loaded, typing fires a
debounced (300ms) GET back to the same `/` route and HTMX swaps in just the
results (`hx-select` + `hx-swap="outerHTML"`) — there is no separate
JSON-for-the-UI endpoint. With HTMX blocked or unavailable, the same form
still submits normally (Enter, or the visible Search button), reloading `/`
with `?q=...` and getting the exact same server-rendered result. `/sboms/{id}`'s
component filter is a few lines of inline vanilla JS; without JavaScript the
full component list is still there, just unfiltered — never a dead control.

Dark mode follows `prefers-color-scheme` automatically (Tailwind v4's default
`dark:` behaviour) — no toggle, no stored preference.

### Regenerating the compiled CSS

`kabom/static/css/style.css` is compiled from `kabom/static/css/input.css` by
the Tailwind **standalone CLI** — a single downloaded binary, not npm. There
is no JS build step and no `node_modules` in the runtime image (see
CLAUDE.md and the Dockerfile's `tailwind` build stage, which recompiles it
fresh from source on every `docker build`).

To regenerate it locally after editing templates or `input.css`:

```bash
./scripts/build-css.sh            # downloads the CLI on first run, then builds once
./scripts/build-css.sh --watch    # rebuild on every change
```

## Try it locally with docker compose

`docker-compose.yml` (repo root) is **dev/test only** — never part of the
production image or the k8s deploy. It builds the same production Dockerfile
and brings up KaBOM against a local MinIO seeded with the committed sample
CycloneDX files (`tests/samples/`), so there is a real, working instance to
click through without touching the real pi10 MinIO.

```bash
docker compose up --build
# open http://localhost:8090/
docker compose down -v            # tear down; -v also drops the seeded data
```

The default seed scenario (`mixed`) uploads both committed samples — one
good, one deliberately corrupted — so the freshness banner comes up **RED**
out of the box: "1 of 2 read." That is intentional, not a bug in the demo: it
is the real failure-reporting behaviour the whole project exists for, and
it's the easiest thing to show accidentally hiding. Two more scenarios are
available for a GREEN or AMBER banner instead:

```bash
KABOM_SEED_SCENARIO=fresh docker compose up --build   # GREEN
KABOM_SEED_SCENARIO=amber docker compose up --build   # AMBER
```

See `scripts/seed_minio.py` for exactly what each scenario uploads.

## End-to-end tests (Playwright)

`tests/e2e/` is a small Node/Playwright project — Node tooling is fine here,
it is a **test suite**, not the runtime image. It drives a real browser
against the docker-compose stack above and covers what unit tests can't:
real typing/debounce, real HTMX swaps, a real mobile viewport, and real
JavaScript-blocked degradation.

```bash
./tests/e2e/run-e2e.sh
```

This installs Playwright's dependencies on first run (`npm install` +
`npx playwright install chromium`), then cycles through all three seed
scenarios — `mixed` (RED + red-border + JS-blocked + all three screens +
keyboard search), `fresh` (GREEN), `amber` (AMBER) — bringing docker compose
up and down around each. Set `PLAYWRIGHT_INSTALL_DEPS=1` before first run if
you also need Playwright's OS-level browser dependencies installed (Linux
only, needs root — this is what a CI runner would do).

To run it by hand against a stack you already have up:

```bash
docker compose up -d --build
cd tests/e2e && npm install && npx playwright install chromium
KABOM_SEED_SCENARIO=mixed npx playwright test    # matches whatever scenario is seeded
cd ../.. && docker compose down -v
```

This runs in CI too (`.github/workflows/ci.yml`'s `e2e` job) — every push
brings the docker-compose stack up for real and runs the full Playwright
suite against it, not just the Python unit tests.

## Status

HOME-228 through HOME-232 are done: S3 ingest, SQLite storage, the search
API, and this UI. Auth (HOME-233) and packaging/deploy (HOME-234) are next.
Today the app serves:

```
GET  /healthz              -> {"status": "ok"}
POST /admin/refresh        -> trigger one ingest pass on demand
GET  /api/search?q=...     -> [{subject, kind, name, version, purl, generated_at}]
GET  /api/sboms            -> [{id, subject, kind, generated_at, component_count}]
GET  /api/sboms/{id}       -> {id, subject, kind, generated_at, components: [...]}
GET  /api/status           -> {finished_at, sboms_seen, sboms_failed, ok, age_seconds}
GET  /                     -> search UI
GET  /sboms                -> inventory UI
GET  /sboms/{id}           -> contents UI
```
