# TAM — Task AutoManager

**Spec v1.0 — the original pre-build spec, kept for the record.**

> This describes what was agreed before implementation. The system has grown
> since: shared storage with a single-writer guard, a remote CLI, a board UI,
> the provider/parser architecture, metrics/targets/params, and push reporting.
> For the system as built see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Owner: user · Target host: local Linux (Asia/Jerusalem) · Date: 2026-08-11

---

## 1. Goal

A Jira-shaped issue database for a single user, driven primarily by an AI agent, with a
daily human checkpoint at 08:00.

Three properties matter more than feature count:

1. **Agent-first.** Every operation is reachable from a non-interactive interface with
   machine-readable output. No operation is UI-only.
2. **Zero dependencies.** Python 3.12 stdlib + SQLite only. The host has no `pip`, no
   `ensurepip`, no `node`. Nothing to install, nothing to break on upgrade.
3. **Auditable.** Every mutation is recorded. When the agent changes 20 issues overnight,
   you can see exactly what it did and why.

### Non-goals (v1)

Multi-user accounts and permissions · sprints, epics, story points, burndown · web UI ·
attachment blob storage (paths/URLs are stored, files are not) · real-time push ·
external Jira/GitHub sync · notifications beyond the daily digest.

---

## 2. Architecture

One service core, three thin adapters. **Adapters contain no business logic** — they parse
input, call core, serialize output. This is the load-bearing rule of the design: it is the
only reason three interfaces can stay behaviourally identical without triplicated tests.

```
   ┌─────────┐      ┌──────────┐      ┌─────────┐
   │   CLI   │      │ HTTP API │      │   MCP   │     adapters (I/O only)
   │ argparse│      │http.server│     │  stdio  │
   └────┬────┘      └────┬─────┘      └────┬────┘
        └────────────────┼─────────────────┘
                   ┌─────▼──────┐
                   │    core    │   workflow rules, validation, audit
                   └─────┬──────┘
                   ┌─────▼──────┐
                   │     db     │   sqlite3, WAL, migrations
                   └────────────┘
```

### Layout

```
task_automanager/
├── SPEC.md
├── README.md
├── bin/
│   ├── tam                     # CLI          -> python3 -m tam.cli
│   ├── tam-api                 # HTTP server  -> python3 -m tam.api
│   └── tam-mcp                 # MCP stdio    -> python3 -m tam.mcp
├── tam/
│   ├── config.py               # data/config.json load/defaults
│   ├── errors.py               # TamError hierarchy
│   ├── db.py                   # connection, pragmas, migration runner
│   ├── migrations/001_init.sql
│   ├── models.py               # dataclasses + enums
│   ├── core/
│   │   ├── projects.py
│   │   ├── issues.py           # create / update / transition / delete
│   │   ├── workflow.py         # transition matrix + guards
│   │   ├── links.py
│   │   ├── comments.py
│   │   ├── query.py            # the one filter/sort engine
│   │   └── events.py           # audit writer
│   ├── digest.py               # daily digest builder
│   ├── cli.py
│   ├── api.py
│   └── mcp.py
├── tests/                      # stdlib unittest
├── deploy/
│   ├── tam-digest.service
│   └── tam-digest.timer
└── data/                       # gitignored
    ├── tam.db
    ├── config.json
    ├── api_token               # 0600
    └── digests/YYYY-MM-DD.{md,json}
```

---

## 3. Data model

SQLite. `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`. WAL plus
busy_timeout is what lets the CLI, the HTTP server and the MCP server safely share one
file. Writes use `BEGIN IMMEDIATE`.

All timestamps are **UTC ISO-8601** (`2026-08-11T06:00:00Z`). Dates (`due_date`) are local
calendar dates (`2026-08-11`) — a due date is a human day, not an instant.

```sql
meta(key TEXT PRIMARY KEY, value TEXT)          -- schema_version

project(
  id INTEGER PK, key TEXT UNIQUE,               -- 'TAM', 2-10 chars [A-Z]
  name TEXT, description TEXT,
  issue_seq INTEGER DEFAULT 0,                  -- per-project counter
  created_at TEXT, archived_at TEXT NULL)

issue(
  id INTEGER PK,
  project_id INTEGER FK->project,
  seq INTEGER, key TEXT UNIQUE,                 -- 'TAM-42'
  type TEXT,        -- task | bug | chore | spike
  title TEXT, body TEXT,
  status TEXT,      -- backlog|todo|in_progress|blocked|review|done|cancelled
  priority TEXT,    -- p0 | p1 | p2 | p3
  assignee TEXT, reporter TEXT,                 -- free-text ('me','agent')
  due_date TEXT NULL,
  parent_id INTEGER NULL FK->issue,             -- subtask
  external_ref TEXT NULL,                       -- URL / file path
  created_at TEXT, updated_at TEXT, closed_at TEXT NULL,
  UNIQUE(project_id, seq))

comment(id INTEGER PK, issue_id FK, author TEXT, body TEXT, created_at TEXT)

label(id INTEGER PK, name TEXT UNIQUE)
issue_label(issue_id FK, label_id FK, PRIMARY KEY(issue_id, label_id))

issue_link(
  id INTEGER PK, from_issue FK, to_issue FK,
  type TEXT,                                    -- see §4.3
  created_at TEXT,
  UNIQUE(from_issue, to_issue, type))

event(                                          -- append-only audit log
  id INTEGER PK, issue_id FK NULL, actor TEXT,
  kind TEXT,        -- created|updated|transitioned|commented|linked|unlinked|deleted
  field TEXT NULL, old_value TEXT NULL, new_value TEXT NULL,
  note TEXT NULL, at TEXT)

digest_run(
  id INTEGER PK, run_date TEXT UNIQUE, generated_at TEXT,
  payload_json TEXT,                            -- the digest snapshot
  reviewed_at TEXT NULL, review_notes TEXT NULL)
```

Indexes on `issue(status)`, `issue(due_date)`, `issue(updated_at)`, `issue(parent_id)`,
`issue(project_id, status)`, `event(issue_id, at)`, `comment(issue_id)`.

Full-text search over `issue.title`/`issue.body` via an FTS5 virtual table kept in sync by
triggers, with a `LIKE`-based fallback if FTS5 is unavailable in the host build.

**Issue keys.** `TAM-42` is allocated by `UPDATE project SET issue_seq = issue_seq + 1
RETURNING issue_seq` inside the creating transaction. Atomic, gapless-on-success, no
races across the three adapters. Keys are never reused, including after delete.

**Migrations.** Numbered `.sql` files applied in order; `meta.schema_version` records the
high-water mark. Startup applies pending migrations inside a transaction. Forward-only —
rollback is "restore the file", which for a single-user SQLite DB is the honest answer.

---

## 4. Domain rules

These live in `core/` and are enforced identically for all three adapters.

### 4.1 Status workflow

| From | Allowed to |
|---|---|
| `backlog` | todo, cancelled |
| `todo` | in_progress, blocked, backlog, cancelled |
| `in_progress` | review, blocked, done, todo, cancelled |
| `blocked` | in_progress, todo, cancelled |
| `review` | done, in_progress, blocked |
| `done` | in_progress *(reopen)* |
| `cancelled` | backlog *(revive)* |

Guards:

- **Open subtasks block closure.** `-> done` fails if any child is not `done`/`cancelled`.
  Overridable with `--force`, which is recorded in the event note.
- **Blocking needs a cause.** `-> blocked` requires either a `--reason` or an existing
  `blocked_by` link. A blocked issue with no stated cause is the single most common way a
  task board rots.
- **Timestamps.** `closed_at` is set on entering `done`/`cancelled` and cleared on reopen.
  `updated_at` is touched on every mutation.
- Illegal transitions raise `TransitionError` (never silently no-op).

### 4.2 Priority

`p0` critical · `p1` high · `p2` medium (default) · `p3` low. Sorts ascending by rank.

### 4.3 Links

| Type | Inverse | Semantics |
|---|---|---|
| `blocks` | `blocked_by` | auto-creates the paired inverse row |
| `relates_to` | *(symmetric)* | stored once, queried both directions |
| `duplicates` | `duplicated_by` | auto-creates the paired inverse row |

`blocks` graph is checked for cycles on insert (`ConflictError`). Self-links rejected.
Deleting an issue removes its links; the audit rows survive.

### 4.4 Audit

Every mutation writes one or more `event` rows carrying actor, field, old and new values.
`actor` defaults to `config.actor` (`"agent"` for CLI/API/MCP, `"daily-review"` during the
08:00 session) and is overridable per call. This is what makes autonomous agent operation
reviewable rather than mysterious.

### 4.5 Query engine

One filter object, used by all adapters and by the digest:

`project` · `status` (multi) · `priority` (multi) · `type` (multi) · `assignee` ·
`label` (multi, AND) · `text` (FTS over title+body) · `due_before` / `due_after` /
`due_on` · `overdue` (bool) · `updated_before` (staleness) · `created_after` ·
`parent` · `has_parent` · `is_blocked` · `include_closed` (default false)

Sort: `priority`, `due_date`, `updated_at`, `created_at`, `key`, `status`; `asc`/`desc`;
`limit`/`offset`. Every filter is expressible in all three interfaces — no adapter gets a
capability the others lack.

---

## 5. Interfaces

### 5.1 CLI (`bin/tam`) — the primary agent surface

Chosen as primary because it needs no running daemon: any agent session can drive it via
Bash immediately.

```
tam init [--project-key TAM] [--project-name "..."]

tam project list|create|show|archive

tam issue create  -t TITLE [-b BODY] [--type task|bug|chore|spike]
                  [-p p0..p3] [-s STATUS] [-a ASSIGNEE] [-d YYYY-MM-DD]
                  [--parent KEY] [-l LABEL ...] [--project KEY] [--ref URL]
tam issue show    KEY [--comments] [--history] [--links]
tam issue list    [--status ...] [--priority ...] [--label ...] [--assignee X]
                  [--due-before D] [--overdue] [--stale DAYS] [--text Q]
                  [--parent KEY] [--include-closed] [--sort F] [--limit N]
tam issue update  KEY [-t] [-b] [-p] [-a] [-d|--clear-due] [--type] [--parent]
tam issue move    KEY STATUS [--reason R] [--force]
tam issue delete  KEY [--yes]
tam issue history KEY

tam comment add   KEY -m "..."          |  tam comment list KEY
tam label add|rm  KEY LABEL             |  tam label list
tam link add      KEY --blocks|--blocked-by|--relates-to|--duplicates KEY2
tam link rm       KEY KEY2 --type T

tam digest [--date YYYY-MM-DD] [--write] [--json]
tam review [--date D]                   # prints digest + open review context
tam stats

# global: --json  --db PATH  --actor NAME  --quiet
```

`--json` on every read command emits a stable envelope — this is how the agent consumes
output; the human table format is explicitly not a parsing target.

```json
{"ok": true, "data": {...}, "meta": {"count": 3}}
{"ok": false, "error": {"code": "not_found", "message": "no issue TAM-99"}}
```

Exit codes: `0` ok · `2` usage · `3` not found · `4` validation · `5` transition denied ·
`6` conflict · `1` unexpected.

### 5.2 HTTP API (`bin/tam-api`)

`http.server.ThreadingHTTPServer`, bound to **127.0.0.1:8787** only. Bearer token from
`data/api_token` (generated at `init`, `0600`, `secrets.token_urlsafe(32)`), required on
every request; constant-time compare. Bodies and responses are JSON, same envelope as
`--json`.

```
GET    /health
GET    /api/projects                 POST /api/projects
GET    /api/issues?<filters>         POST /api/issues
GET    /api/issues/{key}             PATCH /api/issues/{key}     DELETE /api/issues/{key}
POST   /api/issues/{key}/transition  {"status":"...","reason":"...","force":false}
GET    /api/issues/{key}/comments    POST /api/issues/{key}/comments
GET    /api/issues/{key}/history
GET    /api/issues/{key}/links       POST /api/issues/{key}/links   DELETE /api/issues/{key}/links
GET    /api/digest?date=YYYY-MM-DD   POST /api/digest/{date}/review
GET    /api/stats
```

Errors map to `400` validation · `401` bad token · `404` not found · `409` conflict/
transition · `500` unexpected. Query filters use the same names as CLI flags. Served
single-process; SQLite WAL handles the concurrency.

### 5.3 MCP server (`bin/tam-mcp`)

JSON-RPC 2.0, newline-delimited, over stdio. Implements `initialize`,
`notifications/initialized`, `tools/list`, `tools/call`. **stdout carries protocol frames
only** — all logging goes to stderr, which is the usual way a hand-rolled MCP server
breaks.

Tools: `tam_create_issue` · `tam_list_issues` · `tam_get_issue` · `tam_update_issue` ·
`tam_transition_issue` · `tam_add_comment` · `tam_link_issues` · `tam_issue_history` ·
`tam_daily_digest` · `tam_stats`.

Registered in `.mcp.json` at the project root so any Claude Code session in this directory
gets the tools natively.

---

## 6. The 08:00 daily sync

Split deliberately into a part that cannot fail and a part that needs an agent.

### Part A — digest generation (deterministic, 07:55)

Pure stdlib Python, no network, no agent. A `systemd --user` timer (not cron: it gives
`Persistent=true` for missed runs after suspend, plus `journalctl` logs).

```ini
# deploy/tam-digest.timer
[Timer]
OnCalendar=*-*-* 07:55:00        # Asia/Jerusalem
Persistent=true
```

Runs `tam digest --write`, producing `data/digests/YYYY-MM-DD.md` + `.json` and a
`digest_run` row. Sections:

| Section | Rule |
|---|---|
| **Overdue** | `due_date < today`, open — with days late |
| **Due today** | `due_date == today`, open |
| **Due this week** | next 7 days, open |
| **In progress** | status `in_progress`, flagged if over WIP limit |
| **Blocked** | status `blocked`, each with its blocker or reason |
| **Stale** | open, `updated_at` older than `stale_days` (default 7) |
| **Triage queue** | `backlog` with no priority set or no due date |
| **Closed since last digest** | `done`/`cancelled` in the last 24h |
| **Counts** | open by status and priority, plus deltas vs. yesterday |

### Part B — interactive review (agent, 08:00)

The digest is presented; you respond in plain language ("close 13, push 27 to Friday,
drop 31"); the agent applies the changes through the same core, with every write recorded
as `actor='daily-review'`; `digest_run.reviewed_at` and `review_notes` are stamped.
`tam review` re-prints the current digest and flags whether today's has been reviewed.

**Open item — activation mechanism.** `claude` is not on `PATH` here (the VSCode extension
bundles its own). Part A is unaffected. For Part B, pick one at implementation time:

1. **Manual, in-session** — you run `/review` (a small project skill) in Claude Code each
   morning. Zero new infrastructure; depends on you opening a session. *Recommended start.*
2. **Shell-login nudge** — a `.bashrc` line prints today's digest headline and reminds you
   if unreviewed. Complements option 1 well.
3. **Fully automated** — locate/install a `claude` CLI, and have a second systemd timer at
   08:00 run `claude -p "$(tam review)"`. Truly hands-off, but needs the binary resolved
   and a non-interactive auth path.

I suggest shipping 1+2, and adding 3 once we confirm the CLI path.

### Config (`data/config.json`)

```json
{ "default_project": "TAM", "actor": "agent", "timezone": "Asia/Jerusalem",
  "stale_days": 7, "wip_limit": 3, "digest_dir": "data/digests",
  "api_host": "127.0.0.1", "api_port": 8787 }
```

---

## 7. Testing

Stdlib `unittest`, run via `python3 -m unittest discover`. Each test gets a fresh temp DB.

- **Core** (the bulk): key allocation, every legal and illegal transition, subtask-closure
  guard, blocked-needs-reason guard, link inverse creation, cycle rejection, every query
  filter, audit completeness, migration idempotency.
- **Adapters** (thin): one round-trip per interface proving CLI, HTTP and MCP produce the
  same result for the same operation.
- **Digest**: a seeded DB with known dates asserts exact section membership — the one
  place off-by-one date bugs actually hurt.
- **Concurrency**: two writers against one DB, asserting no key collision and no
  `database is locked` failure.

---

## 8. Delivery phases

| # | Deliverable | Gate |
|---|---|---|
| 1 | `db` + `models` + `core` + tests | full workflow suite green |
| 2 | CLI + `tam init` | full lifecycle drivable by hand |
| 3 | Digest + systemd timer | digest file appears at 07:55 |
| 4 | HTTP API | parity tests vs. CLI |
| 5 | MCP server + `.mcp.json` | tools visible in a Claude Code session |
| 6 | README + `/review` skill | daily loop running end to end |

Phases 1–3 give a fully working system; 4–5 add the remaining interfaces. Each phase is
independently usable.

---

## 9. Decisions to confirm

1. Project key **`TAM`** and initial project name — or do you want several projects from
   the start (e.g. `WORK`, `HOME`)?
2. Part B activation: ship **1+2** (manual `/review` + login nudge) now, defer 3?
3. `stale_days=7`, `wip_limit=3` — reasonable defaults for you?
4. Issue types `task|bug|chore|spike` — right set?
5. Anything in §1 non-goals you actually want in v1?
