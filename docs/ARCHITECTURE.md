# TAM architecture

The system as built. [`SPEC.md`](../SPEC.md) is the original pre-build spec and
is kept for the record; where the two disagree, this file is correct.

At a glance: 9,732 lines of Python, 260 tests, no third-party dependencies.
12 application tables, 42 HTTP endpoints,
19 MCP tools, 6 watch providers, 3 metric parsers, 3 systemd timers.

---

## 1. The load-bearing rule

**One service core holds every rule. Every other component is a thin adapter.**

```
   CLI          HTTP API        MCP          Board UI      Remote CLI
   bin/tam      bin/tam-api     bin/tam-mcp  (served by    tam/remote.py
                                              tam-api)
      └──────────────┴──────────────┴─────────────┴──────────────┘
                                  │
                            tam/core/          rules, guards, audit
                            tam/db.py          sqlite + migrations
```

Adapters parse input, call `core.Service`, and serialise the result. They
contain no workflow logic. This is why five surfaces behave identically without
five sets of tests, and it is enforced two ways:

- `tests/test_adapters.py::TestParity` issues the same operation over CLI, HTTP
  and MCP and asserts identical results **and identical refusals**.
- `tests/test_remote.py::TestSurfaceParity` diffs the public method sets of
  `Service` and `RemoteService`, so a method added to one and not the other
  fails the suite. This caught two real gaps.

`RemoteService` deserves note: it presents the same method surface as `Service`
but over HTTP, so `cli.py` runs unchanged against a remote database with no
branching. Remote mode is not a second client.

---

## 2. Storage, and the one hard constraint

SQLite at `$TAM_HOME/data/tam.db` on the serving host's **local disk**. WAL,
`foreign_keys=ON`, `busy_timeout=5000`, explicit `BEGIN IMMEDIATE` for writes.

**Only one host may open the file.** This is measured, not cautious:

| journal mode | two hosts writing concurrently |
|---|---|
| WAL | second host lost **100%** of writes (`disk I/O error`) |
| TRUNCATE | both wrote, **11% of writes lost** (33 of 300) |

WAL keeps coordination state in a shared-memory file that does not exist across
machines; NFS advisory locking is not strong enough to rescue rollback-journal
mode either.

That measurement is also why the database is not on NFS at all any more. Shared
storage could never deliver the thing it looks like it delivers — a second host
opening the file — so it contributed no availability, only a corruption
surface and a tempting mistake. Local disk states the constraint honestly:
there is exactly one writer because there is exactly one copy.

`config.json` still records `db_host`, and `core.check_db_owner` still refuses a
foreign host with exit code 6, but the guard only fires when `db_path` is on a
network filesystem. It is now a backstop for someone pointing `TAM_HOME` back at
a share, not a load-bearing rule. `TAM_ALLOW_FOREIGN_DB=1` lifts it.

Every other node reaches the data through `tam-api` on the serving host. **No
TAM code is published to shared storage**; `/mnt/datasets/tam/env.sh` carries
`TAM_API_URL` and `TAM_API_TOKEN` and nothing else. A node needs an address and
a token, not an install.

Since local disk has no redundancy under it, `backup_dir` points off-host
(`/mnt/datasets/tam/backups`). That is the only thing shared storage is now
trusted with, and it is a write-once artifact rather than a live database.
`env.sh` selects local-file or API mode by hostname, so `tam` behaves the same
either way.

### Tables

| table | purpose |
|---|---|
| `project`, `issue`, `comment`, `label`, `issue_label`, `issue_link` | the board |
| `event` | append-only audit log; survives deletion of its issue |
| `digest_run` | one persisted digest per day, plus review state |
| `watch` | binds an issue to something observable |
| `metric` | numeric observations over time |
| `target` | acceptance criteria as data |
| `param` | run configuration |
| `issue_fts*` | FTS5 index, with a LIKE fallback if unavailable |

Migrations are numbered SQL files, forward-only, each applied inside its own
transaction together with its version bump — so a half-applied migration cannot
be recorded as complete.

---

## 3. Domain rules

Seven statuses with an explicit transition matrix. Two guards carry most of the
weight:

- **Open subtasks block closure.** `→ done` fails while a child is open;
  `force` overrides and records that it did.
- **Blocking requires a cause.** `→ blocked` needs a reason or a `blocked_by`
  link. Enforced on creation too.

Status changes go only through `transition()`, never `update()`, so the matrix
cannot be bypassed. Links are stored as inverse pairs with cycle detection on
the `blocks` graph. Subtask nesting is one level deep.

Every mutation writes an `event` row with actor, field, old and new value. This
is what makes autonomous agent operation reviewable rather than mysterious.

---

## 4. Observation: watches, providers, parsers

The half of the system that watches work rather than recording it.

### Providers

A watch names a **provider**. The core knows nothing about Slurm, GCP or
Encord — it knows a provider can be asked to observe a reference and answer in
one canonical vocabulary:

```
running · pending · succeeded · failed · stopped · present · missing ·
unreachable · unknown
```

Everything downstream — the digest's attention list, alerts, queries — works off
those words. A provider keeps its own term in `native_state` for display;
nothing branches on it.

Shipped: `slurm`, `process`, `path`, `heartbeat`, and the two general ones —
`command` (run anything, map output to state and metrics) and `http` (GET a URL,
read state and metrics from the response). Between them they cover any tool with
a CLI or REST API **by configuration rather than code**.

New tools are added two ways, neither of which touches the core:

1. Configure `command` or `http` on the watch.
2. Drop a `.py` into `$TAM_HOME/data/providers/`; it is loaded by the CLI, API,
   MCP and scan timer alike. A provider that fails to load warns and is skipped.

### Parsers

Probing ("is it running?") and measuring ("what is recall now?") are separate
concerns, so a new log format is a parser rather than a provider change.
Built in: `yolo`, `regex` (your patterns), `json` (dotted paths), selected per
watch via `config.parser`.

### The scanner

`tam scan` resolves each watch's provider, probes, stores the canonical state,
records metrics, and **comments on the issue when the state changes**. A
provider that raises is caught and recorded as `unknown` rather than aborting
the sweep.

Two hard-won details:

- Metrics are deduplicated on `(issue, name, step, source)` and identical
  consecutive values are skipped, or a 15-minute timer against an idle source
  would grow the series forever.
- A watch may carry `state_patterns` that override the scheduler's verdict from
  the log. Slurm reports `COMPLETED` whenever the wrapper script exits 0 —
  including when the training inside it was killed.

### Metrics and targets

`target` expresses acceptance criteria as data (`recall >= 0.90`), evaluated
against the latest metric. Targets **fall back to the parent issue's metrics**:
a job is watched on the parent that owns it while the criteria live on its
subtasks.

`metric_trend` fits a least-squares line and tests the slope against its own
standard error, reporting a direction only when the fit beats the scatter.
Comparing endpoints is not good enough — a metric oscillating around a fixed
value produces an endpoint delta whose sign is arbitrary, and reading that as a
trend has already driven a wrong conclusion here.

### Push reporting

For work TAM cannot observe — cloud VMs, hosts behind NAT, metrics that only
exist inside a running process — jobs report in via `POST /heartbeat`.
`clients/tam_report.py` is a ~40-line stdlib client for training scripts. The
`heartbeat` provider turns silence beyond a timeout into `stopped`, because a
pushing job that dies simply stops pushing and is otherwise indistinguishable
from one working quietly.

**Reporting never breaks the job it reports on**: every client call swallows its
errors and warns on stderr.

---

## 5. Scheduling

`systemd --user` timers, not cron — for `Persistent=true` (a missed run fires on
wake) and `journalctl` logging.

| timer | when | what |
|---|---|---|
| `tam-scan` | every 15 min | probe watches, record metrics, comment on changes |
| `tam-digest` | 07:55 daily | build and persist the daily digest |
| `tam-backup` | 02:30 daily | consistent snapshot, 14 retained |

Backups use SQLite's online backup API rather than copying the file — a plain
copy of a live WAL database can capture a torn state. Each snapshot is
integrity-checked and discarded if it fails.

The digest is deliberately dependency-free and offline so the record of "what
the board looked like this morning" never has a gap, whether or not an agent is
available.

---

## 6. Security posture

Single shared bearer token, generated at `init` with mode 600, constant-time
compared. The board UI is served with the token embedded because the browser has
no other way to obtain it — safe **only** because no CORS headers are ever sent,
so a page on another origin cannot read the response. Requests carrying a
foreign `Host` header are rejected to block DNS rebinding. Both properties are
covered by tests; do not add `Access-Control-Allow-Origin`.

Provider credentials are named by environment variable (`token_env`), never
stored in the database.

This is a private-network posture: plain HTTP, one shared secret, no rate
limiting, no per-caller identity.

---

## 7. Deliberate omissions

**No optimistic concurrency, no atomic claim, no idempotency keys.** Agents are
assumed to work on disjoint issues. A live test confirmed two simultaneous
writes to one issue both succeed and the later one wins silently — a real
lost-update hole that only bites under contention.

If agents ever share issues, revisit in this order: `If-Match` on `updated_at` →
atomic claim → idempotency keys. `GET /api/events?since=` already exists as a
change feed, so most of the coordination primitive is built.

Also absent by choice: multi-user accounts, sprints and story points, attachment
blob storage, real-time push, external Jira/GitHub sync.

---

## 8. Known risks

**Single point of failure.** Every agent write depends on the `db_host`, which
is currently a development box that Slurm reports as `down`. There is no
failover. Moving the API to a stable host is a config change and a systemd unit.

**Hostname/DNS disagreement.** The owner host calls itself `db-host` while
DNS maps that name elsewhere; the API is reachable at a different address. The
ownership guard compares hostnames and assumes they are unique.

**Lingering.** `systemd --user` timers only run while a user manager is alive.
Without `loginctl enable-linger`, they stop when you log out.

**No per-agent identity.** `TAM_ACTOR` sets the actor recorded in the audit log, defaulting to `agent@<hostname>` when unset,
but it is self-declared and unverifiable. The log is honest only if the agents
are.

---

## 9. Documentation map

| file | audience |
|---|---|
| [`README.md`](../README.md) | operators — install, run, daily use |
| [`docs/API.md`](API.md) | HTTP reference, generated from the route table |
| [`docs/ARGUS.md`](ARGUS.md) | agents — how to record work |
| [`docs/SLURM.md`](SLURM.md) | agents — how to run cluster jobs |
| [`SPEC.md`](../SPEC.md) | historical — the original approved spec |
| this file | maintainers — why it is shaped this way |

`docs/API.md` is generated by `tools/gen_api_docs.py`; run it with `--check` to
fail when it drifts.
