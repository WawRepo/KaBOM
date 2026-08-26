# CLAUDE.md — KaBOM

💥 **KaBOM** — *"Your SBOMs, without the enterprise."*

A small web app that reads CycloneDX SBOMs out of MinIO and lets one human
search them. Built for a 12-node Raspberry Pi homelab, not for a company.

> **This file is rules and pointers only.** What to build is in the Plane
> tickets — read them, do not work from this file's summary.

---

## The one rule that governs every decision

**Every time a choice appears between "scales" and "simple", take simple.**

This is a homelab tool. It holds **27 SBOM files and serves one user**. It
exists because OWASP Dependency-Track is the right tool and the wrong size —
8 GB RAM, 4 cores and its own Postgres, on a box with 4 GB free.

If you find yourself adding any of these, stop and re-read the parent ticket:

| Not building | Because |
|---|---|
| Multi-tenancy, teams, RBAC | One user. Two if generous. |
| A policy engine, VEX workflows | That is Dependency-Track's job. |
| **Its own scanner** | **KaBOM never scans anything.** Grype does that elsewhere. |
| Postgres, Redis, a queue | 27 files. SQLite, one process. |
| Pagination, caching layers, sharding | The whole dataset fits in RAM many times over. |

## The thing KaBOM must never do

**Show stale data as though it were current.**

An SBOM is a point-in-time claim. If the job that generates them breaks, KaBOM
will happily serve last month's contents and answer *"no, we don't have that
package"* with total confidence — and be wrong. Someone acts on that answer
during an incident.

So **every screen shows how old the data is, prominently, and turns red when it
is stale.** Not a footer. Not a tooltip. This is a hard requirement, it appears
in three separate tickets, and it is the first thing to check in review.

The age shown is always that of the **oldest** SBOM, never the newest and never
the average — one file stuck at 40 days while 26 refresh nightly is exactly the
case that matters, and the other two hide it.

---

## Issue tracking — Plane (`HOME-*`)

**All work here is tracked in Plane**, the same board as the `pi_cluster`
homelab repo. Self-hosted at <https://plane.szatanik.dev>.

**The `plane` MCP server is configured globally**, so it works in this repo with
no setup. Use it directly; do not scrape the web UI.

### The IDs you need (saves several lookups)

```
project_id   ad6ad66a-2370-435f-aa56-84ed8ce4a0b6      (project "home", prefix HOME)

states       Backlog      707ceae0-2018-4905-a322-9865277838f3
             Todo         479a2a41-9682-4173-ac5f-e3aa379ea388
             In Progress  6a54016b-5707-4520-8749-d62b32d1cdc9
             Done         465dacb2-0885-4740-8ebc-26fba424dc07
             Parked       2fe6e35d-33be-4fe4-82ce-0ad3cf4d5b77
             Cancelled    06054609-0f90-4465-b266-a98681cc9a41
```

### This project's tickets

`HOME-227` is the epic — **read it first, it carries the design decisions**
so the sub-tasks do not each re-argue them.

| # | Ticket | What |
|---|--------|------|
| 1 | HOME-228 | Repo skeleton, stack, CI |
| 2 | HOME-229 | Read SBOMs from MinIO (S3) |
| 3 | HOME-230 | SQLite schema and ingest |
| 4 | HOME-231 | The search API — the one real feature |
| 5 | HOME-232 | The UI, and the freshness banner |
| 6 | HOME-233 | Auth — basic, then Google |
| 7 | HOME-234 | Package and deploy into the cluster |

**Order is fixed: 1 → 2 → 3 → 4 → 5, then 6 and 7 in either order.**
Do not start 5 before 4 exists — a UI built against an imagined API is rework.

### Working the board

- **Move the child to `In Progress` as you pick it up**, never retroactively.
  The board is how a resumed session sees what is mid-flight, and a `Todo` item
  that is already half-written is actively misleading.
- **One child `In Progress` at a time.** Finish it or park it before the next.
- **Read the child's own description before claiming it.** Each names install
  paths, variable names and acceptance criteria the epic does not.
- **Move to `Done` only after the acceptance criteria actually passed** — not
  when the code compiles. Several criteria say "proven, not assumed"; those mean
  run it and look.
- Prefix every commit with the ticket: `feat(api): search by package name (HOME-231)`.
- **Found a bug while doing something else? File it separately**, do not widen
  the ticket in flight.

Practical notes on this Plane edition:
- `list_work_items` **rejects PQL** — fetch and filter client-side.
- `description_html` is real HTML. Pre-escaping it double-escapes and the ticket
  shows raw tags.
- Park work with the **`Parked`** state, never `Cancelled` — they mean opposite
  things.

---

## Delegate the sub-tasks to Sonnet subagents

**Each ticket was deliberately sliced to be one commit, one context, and no
cross-cutting judgement** — precisely so a cheaper model can execute it from the
ticket alone. Use that.

```
Agent(
  subagent_type: "general-purpose",
  model: "sonnet",
  description: "KaBOM 3/7 SQLite ingest",
  prompt: "Read Plane ticket HOME-230 and its parent HOME-227 via the plane MCP
           (project ad6ad66a-2370-435f-aa56-84ed8ce4a0b6). Implement exactly what
           HOME-230 specifies, in this repo. Follow CLAUDE.md. Run the tests.
           Report what you did and which acceptance criteria you verified.
           Do not commit — the main session reviews first."
)
```

- **One agent per ticket, one at a time.** They build on each other; parallel
  agents on 3 and 4 will conflict.
- **The main session reviews and commits.** A subagent reporting "done" is a
  claim, not evidence — check the acceptance criteria yourself, especially the
  ones that say *proven*.
- **Give the agent the ticket ID, not a summary of it.** Summarising the ticket
  into the prompt is how the spec quietly drifts.
- Escalate to the stronger model for anything the ticket left genuinely open —
  but the tickets were written so that should be rare. If a sub-task needs
  judgement, that is a sign the ticket is underspecified: fix the ticket.

---

## Stack — decided in HOME-227, do not re-litigate per ticket

- **Python 3.12+, FastAPI, `uv`.** Every script in the homelab is Python; a
  second language for one app is a tax paid for ever.
- **SQLite.** Plain `sqlite3` and a `schema.sql`. No ORM, no migrations — if the
  schema changes, drop and re-ingest, because the source of truth is S3 and a
  full rebuild takes seconds.
- **Jinja2 + HTMX + Tailwind (standalone CLI).** Modern and quick to use with
  **no JavaScript build step in the runtime image** — on arm64 that is a real
  cost for no benefit.
- **`ruff`** for lint and format. **`pytest`** for tests.

## Traps carried over from the homelab — each one has already bitten

| Trap | Detail |
|---|---|
| **SQLite on SMB** | **Never put the database on `nas-smb`.** SMB gives no POSIX locking; SQLite corrupts. It goes on `local-path`, which pins KaBOM to one node — the same trade Immich's Postgres and Gitea already make. Write the reason in a comment or someone will "improve" it onto the NAS. |
| **Image architecture** | The cluster is 11 arm64 Pis and one amd64 box. **A single-arch image fails at runtime, not at deploy** — build multi-arch. |
| **Secrets in env vars** | `env_file` does **not** keep secrets out of `docker inspect`. Never a plaintext password in an environment variable; a bcrypt hash is fine. |
| **A generated secret** | Never generate a session secret at startup. It logs everyone out on restart, which gets "fixed" by hardcoding one. Refuse to start instead. |
| **OAuth with no allow-list** | An OAuth app that accepts *any* Google account looks identical to a working one. Explicit email allow-list, and check `email_verified`. |
| **Tests that need the network** | Tests must pass offline with no credentials. Use committed sample CycloneDX files, never a live MinIO. |

## Git

- Remote: `git@github.com:WawRepo/KaBOM.git` (SSH key `~/.ssh/private_waw2`).
- Commit and push to `main` after each working change.
- **The repo is mirrored into Gitea automatically**, read-only, within 15
  minutes — nobody needs to configure that, and nothing here should try to.

## Commands

```bash
uv sync                  # install
uv run pytest            # tests
uv run ruff check .      # lint
uv run ruff format .     # format
uv run uvicorn kabom.main:app --reload    # dev server
docker build .           # the image CI also builds
```

## Where KaBOM's data comes from — you do not build any of this

```
  HOME-224  a nightly job runs `syft` over 15 images + 12 hosts
              │
              ▼  writes CycloneDX JSON
          MinIO on pi10  ──────────►  KaBOM reads it, read-only, and never writes
              ▲
  HOME-225  a nightly `grype` job reads the same files for vulnerabilities
            and reports separately — NOT through KaBOM
```

KaBOM is a **reader**. It does not generate SBOMs, it does not scan for
vulnerabilities, and it has no opinion about what is dangerous. It answers one
question: **"do we have this package, and where?"**
