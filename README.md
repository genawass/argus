# Argus

A Jira-shaped issue database for one person, driven by an AI agent, with a daily
human checkpoint at 08:00.

Named for Argus Panoptes, the hundred-eyed watchman who never slept with every
eye at once: the scanner probes every watched job, path and process on a timer
and records what changed, so the board stays current without being told.

The command stays `tam` and issue keys stay `TAM-*`. Renaming those would dangle
every cross-reference already written into the history — comments say things
like "still blocked by TAM-19", and no migration rewrites prose.

Python 3.12 standard library and SQLite. **No dependencies, nothing to install.**

Architecture and the reasoning behind it: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). HTTP reference: [docs/API.md](docs/API.md). Agent instructions: [docs/ARGUS.md](docs/ARGUS.md), served live at `GET /docs/ARGUS.md`.

---

## Quick start

```sh
./bin/tam init                        # database, config, first project, API token

./bin/tam issue create -t "Renew TLS certs" -p p0 -d +7d -l ops
./bin/tam issue list
./bin/tam issue move TAM-1 todo
./bin/tam issue show TAM-1 --all
./bin/tam digest
```

## How it fits together

One service core holds every rule. Three adapters expose it. **Adapters contain
no business logic** — which is why the CLI, the HTTP API and the MCP tools
behave identically, and why `tests/test_adapters.py::TestParity` can prove it.

```
   CLI            HTTP API         MCP
   bin/tam        bin/tam-api      bin/tam-mcp
      └───────────────┼───────────────┘
                 tam/core/          rules, guards, audit
                 tam/db.py          sqlite + migrations
```

Everything mutable lives in `$TAM_HOME/data`: the database, `config.json`, the
API token, and one markdown + JSON digest per day. `TAM_HOME` defaults to the
checkout (so `./bin/tam init` just works) and is set to `~/.local/share/tam` on
the serving host by [`.tam-env`](.tam-env) — off the shared filesystem, and out
of reach of a `git clean`.

---

## The CLI

The primary agent surface — no daemon required. Every read command takes
`--json` and emits a stable envelope. **That envelope is the contract for
programmatic callers; the human table is not a parsing target.**

```json
{"ok": true,  "data": {...}, "meta": {"count": 3}}
{"ok": false, "error": {"code": "not_found", "message": "no issue TAM-99"}}
```

Exit codes: `0` ok · `2` usage · `3` not found · `4` validation ·
`5` transition denied · `6` conflict · `7` unauthorized · `1` unexpected.

```sh
tam issue create -t TITLE [-b BODY] [--type task|bug|chore|spike]
                 [-p p0..p3] [-s STATUS] [-a ASSIGNEE] [-d DATE]
                 [--parent KEY] [-l LABEL ...] [--ref URL]
tam issue list   [--status ...] [--priority ...] [--label ...] [--assignee X]
                 [--overdue] [--stale DAYS] [--text Q] [--due-before DATE]
                 [--parent KEY] [--include-closed] [--sort F] [--limit N]
tam issue tree   [same filters as list]     # parents with subtasks nested
tam issue show   KEY [--all]          tam issue history KEY
tam issue update KEY [-t] [-p] [-d|--clear-due] [-l ...]
tam issue move   KEY STATUS [--reason R] [--force]
tam issue delete KEY --yes

tam comment add KEY -m "..."          tam label add|rm KEY LABEL...
tam link add KEY --blocks|--blocked-by|--relates-to|--duplicates KEY2
tam digest [--write]                  tam review [--done "notes"]
tam stats                             tam nudge         tam config show
```

Dates accept `YYYY-MM-DD`, `today`, `tomorrow`, `yesterday`, `+3d`, `-2d`.

Global flags (`--json`, `--db`, `--home`, `--actor`, `--quiet`) work **before or
after** the subcommand. `--home` moves the whole install root (database, config,
digests); `--db` moves only the database file.

## Workflow rules

```
backlog ──→ todo ──→ in_progress ──→ review ──→ done
   ↓         ↕            ↕            ↕         ↓
cancelled ←──┴── blocked ─┘            └─────→ in_progress (reopen)
```

Two guards do most of the work of keeping the board honest:

- **Open subtasks block closure.** `→ done` fails while a child is still open.
  `--force` overrides and records that it did.
- **Blocking requires a cause.** `→ blocked` needs a `--reason` or a
  `blocked_by` link. A blocked issue with no stated cause is the most common way
  a task board rots. This holds on creation too.

Status can only change through `move`/`transition` — never through `update`, so
the matrix cannot be bypassed.

Every mutation writes an audit row: who, what field, old value, new value, when.
`tam issue history TAM-42` reads it back, and it survives deletion of the issue.

---

## HTTP API

```sh
./bin/tam-api                       # 127.0.0.1:8787
```

Bearer token from `data/api_token`, generated at `init` with mode 600:

```sh
curl -H "Authorization: Bearer $(cat data/api_token)" \
     'http://127.0.0.1:8787/api/issues?overdue=1'
```

```
GET    /health                          (no auth)
GET  · POST   /api/projects
GET  · POST   /api/issues
GET  · PATCH · DELETE  /api/issues/{key}
POST   /api/issues/{key}/transition     {"status":"done","reason":"","force":false}
GET  · POST   /api/issues/{key}/comments
GET  · POST · DELETE   /api/issues/{key}/links
GET    /api/issues/{key}/history
GET    /api/digest[?date=&write=1]      POST /api/digest/{date}/review
GET    /api/stats                       GET  /api/labels
```

Query filters use the same names as the CLI flags. Errors map to
`400` validation · `401` bad token · `404` not found · `409` conflict or
transition denied.

**This binds to loopback by design.** It is a single-user local service with no
per-user permissions; `--host 0.0.0.0` would expose your whole issue database to
the network behind one shared token. The server warns if you do it anyway.

## Board UI

`tam-api` serves a board at <http://127.0.0.1:8787/> — one self-contained HTML
file, no CDN and no build step. It is another thin adapter: it calls the same
JSON endpoints and enforces nothing itself.

```sh
./bin/tam-api            # then open http://127.0.0.1:8787/
```

- A KPI row across the top: open count, in-progress against the WIP limit as a
  meter, targets met, and jobs needing attention with direct links.
- Columns by status, cards showing priority, due date (red when late), labels,
  assignee, subtask parent, watch state, and a **sparkline** of the issue's
  headline metric where one exists.
- The drawer carries a **metric chart** with crosshair and tooltip, the target
  drawn as a dashed reference line, a metric selector and a table view. The `in progress` column turns amber over the WIP
  limit, and `cancelled` stays hidden until something lands in it.
- **Drag a card to move it.** Illegal targets dim while dragging — the board
  fetches the transition matrix from `/api/workflow` rather than keeping its own
  copy, so it can never disagree with the server.
- The guards still apply, and the UI surfaces them instead of routing around
  them: dropping into `blocked` asks for a reason, and closing a parent with
  open subtasks names them and asks before forcing.
- Click a card for the detail drawer: edit fields inline, comment, read the full
  audit history, delete. `#TAM-42` in the URL deep-links straight to an issue.
- Keyboard: `/` search, `n` new, `r` refresh, `Esc` close. Cards are tabbable and
  open with Enter, since drag-and-drop alone is not accessible.
- Refreshes every 30s, but never while a drawer is open or a drag is in flight,
  so it cannot stomp what you are typing.

State and priority are never carried by colour alone — each ships with a glyph
and a text label. This is measured, not stylistic: status red against green is
ΔE 4.1 under deuteranopia, and amber sits at 1.79:1 on the light surface.
Charts are single-series throughout, so no categorical palette is in play.

The page is served with the API token embedded, because the browser has no other
way to get it. That is safe **only** because no CORS headers are ever sent, so a
page on another origin cannot read the response — and requests carrying a
foreign `Host` header are rejected outright to block DNS rebinding. Both
properties are covered by tests; do not add `Access-Control-Allow-Origin`.

## MCP server

`.mcp.json` registers `tam` for any Claude Code session in this directory, so
the agent gets native tools instead of Bash quoting:

`tam_create_issue` · `tam_list_issues` · `tam_get_issue` · `tam_update_issue` ·
`tam_transition_issue` · `tam_add_comment` · `tam_link_issues` ·
`tam_issue_history` · `tam_daily_digest` · `tam_stats`

Restart Claude Code to pick it up. Verify by hand with:

```sh
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ./bin/tam-mcp
```

---

## Watching work, not just recording it

Bind an issue to something observable and a timer keeps it current, instead of
someone hand-running `squeue`.

```sh
tam provider list                       # what can be watched
tam watch add TAM-1 slurm 38574 --config '{"parser":"yolo"}'
tam watch add TAM-10 path /mnt/datasets/example-corpus --host worker-01
tam scan                                # probe everything now
```

`tam scan` runs every 15 minutes from a timer. It **comments on an issue when
its watched state changes**, so a job that dies is visible without anyone
noticing a log stopped growing. It is read-only against everything it observes.

### Providers: the core knows no tools

TAM contains no knowledge of Slurm, GCP or Encord. A watch names a *provider*,
and providers answer in one canonical vocabulary:

```
running · pending · succeeded · failed · stopped · present · missing ·
unreachable · unknown
```

Everything downstream — the digest's attention list, alerts, queries — works off
those words, so no tool's vocabulary leaks into the core. A provider keeps its
own word in `native_state` for display; nothing branches on it.

Shipped: `slurm`, `process`, `path`, and the two general ones — **`command`**
(run anything, map output to state and metrics) and **`http`** (GET a URL, read
state and metrics from the response). Between them they cover any tool with a
CLI or a REST API, by configuration rather than code:

```sh
tam watch add TAM-4 command 'gcloud ai custom-jobs describe JOB --format=json' \
  --config '{"state_patterns": {"running": "JOB_STATE_RUNNING",
                                "succeeded": "JOB_STATE_SUCCEEDED",
                                "failed": "JOB_STATE_(FAILED|CANCELLED)"},
             "parser": "json", "paths": {"loss": "trainingMetrics.loss"}}'

export ENCORD_TOKEN=...        # named, never stored in the database
tam watch add TAM-10 http 'https://api.encord.com/v1/<endpoint>' \
  --config '{"token_env": "ENCORD_TOKEN", "state_path": "status",
             "state_map": {"IN_PROGRESS": "running", "COMPLETE": "succeeded"},
             "parser": "json", "paths": {"labelled": "stats.labelled"}}'
```

When a tool needs real logic, write a provider: drop a `.py` file into
`$TAM_HOME/data/providers/` and it is available to the CLI, API, MCP and the
scan timer with no change to TAM. See [examples/providers/](examples/providers/).

### Parsers

Probing ("is it running?") and measuring ("what is recall now?") are separate,
so a new log format is a parser rather than a change to a provider. Built in:
`yolo`, `regex` (your patterns), `json` (dotted paths). Chosen per watch via
`--config '{"parser": ...}'`.

### Metrics and targets

```sh
tam metric add TAM-1 recall 0.803 --step 80
tam metric trend TAM-1 recall              # rising / falling / FLAT
tam target set TAM-5 recall '>=' 0.90      # "Accuracy above 90" as data
tam target list                            # met / not met, and by how much
```

Metrics are deduplicated on `(issue, name, step, source)` and the scanner skips
values identical to the last recorded one — otherwise a 15-minute timer against
an idle source grows the series forever.

Targets fall back to the **parent** issue's metrics. A job is watched on the
parent that owns it while the acceptance criteria live on its subtasks ("PD
above 90" under "Model A"); without the fallback every such target would read
"no data" despite the metric existing one level up.

The daily digest reports unmet targets and any watch in an attention state.

## Reporting from other machines

The database is central; work is not. Anything that can reach the API can
report into it, so an agent does not have to run on every machine — you query
one place and see everything.

**Pull** covers what the owner host can observe (Slurm, paths, processes,
commands, HTTP). **Push** covers what it cannot: cloud VMs, machines behind
NAT, or metrics that only exist inside a running process.

### In a training script

Copy [`clients/tam_report.py`](clients/tam_report.py) next to your code —
standard library only, nothing to install:

```python
from tam_report import Reporter

with Reporter("TAM-14", ref="example-finetune") as run:
    run.params(lr=0.001, batch=24, dataset="example_dataset", commit=sha)
    for epoch in range(epochs):
        ...
        run.metric(step=epoch, recall=r, precision=p, map50=m)
```

The context manager reports `succeeded` on exit, or `failed` with the exception
if the job dies — then re-raises. **Reporting never breaks the job it reports
on**: every call swallows its errors and warns on stderr, so a TAM outage costs
telemetry, not a training run.

Configure with `TAM_API_URL` and `TAM_API_TOKEN`; unset means reporting is
silently disabled, which is what you want on a laptop.

### Liveness

A pulled watch reports `stopped` when its process dies. A *pushing* job that
dies just stops pushing — indistinguishable from working quietly. So a
heartbeat records the time of each report, and the `heartbeat` provider turns
silence into a state:

```sh
tam heartbeat TAM-14 train-loop --metric recall=0.81 --step 42
tam heartbeat TAM-14 train-loop --state succeeded
```

Silence beyond `timeout_seconds` (default 900) becomes `stopped` and appears in
the digest's attention list. A job that reported its own ending is finished, not
silent, so it stays `succeeded`.

### Params

```sh
tam param set TAM-14 lr=0.001 batch=24 dataset=example_dataset
tam param list TAM-14
```

Metrics answer "what did it score"; params answer "what was it configured
with". Without both, "which learning rate produced the best recall" is
unanswerable. Types round-trip — ints, floats, booleans, strings and JSON.

Agents get the whole picture in one call via the `tam_get_experiment` MCP tool:
issue, params, latest metrics, target status and what is currently running.

## Backups

```sh
tam backup                     # 14 kept, nightly at 02:30
```

Uses SQLite's online backup API rather than copying the file: a plain copy of a
live WAL database can capture a torn state. Each snapshot is integrity-checked
before old ones are pruned, and discarded if the check fails.

Destination is `backup_dir` in `config.json`, and it should name **another
filesystem** — here `/mnt/datasets/tam/backups`. The database lives on one
host's local disk, so a backup beside it survives a bad migration and nothing
else. Restore is a file copy:

```sh
systemctl --user stop tam-api.service
cp /mnt/datasets/tam/backups/tam-<stamp>.db "$TAM_HOME/data/tam.db"
systemctl --user start tam-api.service
```

## The 08:00 daily sync

Deliberately split into a part that cannot fail and a part that needs an agent.

**07:55 — digest generation.** A `systemd --user` timer runs
`tam digest --write`: pure local computation, no network, no agent. It writes
`data/digests/YYYY-MM-DD.{md,json}` and a `digest_run` row.

```sh
./deploy/install-timer.sh
systemctl --user list-timers tam-digest.timer
```

`Persistent=true` means a machine asleep at 07:55 still builds the digest on
wake, so the record has no holes. systemd user timers only run while your user
manager is alive — for a laptop, enable lingering:

```sh
sudo loginctl enable-linger $USER
```

**08:00 — the review.** Open Claude Code here and run `/tam-review`. It presents
the digest, turns plain language ("close 13, push 27 to Friday, drop 31") into
database changes attributed to `actor=daily-review`, and closes the day out with
`tam review --done "..."`.

Optional login reminder — prints one line when something is overdue, due today
or blocked, and nothing once the day is reviewed:

```sh
cat deploy/bashrc-nudge.sh >> ~/.bashrc
```

Sections in the digest: overdue · due today · due this week · in progress ·
blocked (with blockers or reason) · stale (no activity in `stale_days`) ·
triage queue (backlog, undated, still at default priority) · closed since last
digest · counts with a delta against yesterday.

### Why not fully automatic?

Running the review unattended needs a `claude` CLI on `PATH`, which this machine
does not have — the VSCode extension bundles its own. Digest generation is
unaffected. To automate the review later, add a second timer at 08:00 running
`claude -p "$(tam review)"` once that binary is resolved.

---

## Cluster access

One host serves; everyone else is an HTTP client.

```
worker-node02 (serving host)              any other node
  ~/.local/share/tam/data/tam.db          HTTP + bearer token
  tam-api on :8787                          • browser  -> /board
  digest / scan / backup timers             • curl     -> /api/*
       |
       +-- ssh outward to probe watched jobs
```

**No TAM code is published to shared storage.** `/mnt/datasets/tam/env.sh`
carries two variables and nothing else:

```sh
source /mnt/datasets/tam/env.sh    # TAM_API_URL + TAM_API_TOKEN
curl -H "Authorization: Bearer $TAM_API_TOKEN" "$TAM_API_URL/api/issues"
curl -H "Authorization: Bearer $TAM_API_TOKEN" "$TAM_API_URL/docs/ARGUS.md"
xdg-open "$TAM_API_URL/"           # the board UI — zero install
```

That is the whole client story on a node that has nothing installed. If you also
want the `tam` CLI there, clone the repo and set `TAM_API_URL`; `--api URL`
selects remote mode explicitly, and the token resolves from `TAM_API_TOKEN`,
else `TAM_API_TOKEN_FILE`, else `$TAM_HOME/data/api_token`.

Agent instructions are served too, rather than staged on the share:
`GET /docs` lists `docs/*.md`, `GET /docs/ARGUS.md` returns one. A node reads
its instructions from the same host that enforces them, so the two cannot
disagree about which version is current.

### Why the database is not on shared storage

**Only one host may open a SQLite database file.** This is not a policy choice,
it is measured. Two hosts writing the same file on this NFSv4 mount:

| journal mode | result |
|---|---|
| WAL (default) | second host lost **100%** of writes — `disk I/O error` |
| TRUNCATE | both hosts wrote, **11% of writes lost** (33 of 300) |

WAL keeps coordination state in a shared-memory file that does not exist across
machines, and NFS advisory locking is not strong enough to save rollback-journal
mode either. Neither is safe for a database you expect to keep your task edits.

Since a second host could never open the file anyway, putting it on NFS bought
no availability — only a corruption surface and an invitation to try. It now
lives on the serving host's local disk, which says the same thing more honestly:
one writer, one copy, one machine to back up.

The ownership guard remains as a backstop. `config.json` records `db_host`, and a
foreign host is refused with exit code 6 rather than silently dropping writes:

```
error: /mnt/datasets/tam/data/tam.db is owned by db-host; this is
worker-01. Opening a shared SQLite database from a second host loses writes.
```

It only fires when the database is on a network filesystem, so in the normal
local-disk setup you will never see it. `TAM_ALLOW_FOREIGN_DB=1` lifts it. To
move ownership permanently, stop the API on the old host, copy the file, and set
`db_host` in `data/config.json`.

### Where things live

| path | what |
|---|---|
| `~/dev/argus` | the code — a git checkout, on each host that runs it |
| `$TAM_HOME` (`~/.local/share/tam`) | database, config, token, digests — local disk |
| `/mnt/datasets/tam/env.sh` | `TAM_API_URL` + `TAM_API_TOKEN` for clients (from [`deploy/env.sh`](deploy/env.sh)) |
| `/mnt/datasets/tam/api_token` | the token clients read; rotate here |
| `/mnt/datasets/tam/backups` | off-host snapshots, nightly |

That is everything on shared storage: an address, a token, and snapshots. No
code, no database, no documents.

`.tam-env` in the checkout resolves `TAM_HOME` for `bin/tam`, `bin/tam-api` and
`bin/tam-mcp` alike, so a bare cron line and an interactive shell address the
same database. Set `TAM_ENV` to point at a different file.

Remote mode is not a second client. `tam/remote.py` presents the same method
surface as `core.Service` over HTTP, so `cli.py` runs against it unchanged and
holds no remote-specific branches. Validation, the transition matrix and the
guards still execute on the server; `tests/test_remote.py` drives the same
operations both ways and asserts they agree, including that `--actor` survives
the network hop so remote writes are attributed correctly.

`tam init` is refused over `--api`: it creates a database, so it must run on the
owner host.

### Services on the owner host

```sh
./deploy/install-timer.sh                     # 07:55 digest
systemctl --user enable --now tam-api         # the API other nodes depend on
```

The API listens on `api_host` from config (`0.0.0.0` to serve the cluster) and
answers only to Host headers matching addresses it discovered at startup, plus
anything in `api_allowed_hosts`. That keeps the DNS-rebinding defence intact
while serving the LAN. It is protected by the bearer token only — appropriate
for a private cluster network, not for an untrusted one.

## Configuration

`data/config.json`, created by `init`:

```json
{ "default_project": "TAM", "actor": "agent", "timezone": "Asia/Jerusalem",
  "stale_days": 7, "wip_limit": 3, "digest_dir": "data/digests",
  "backup_dir": "/mnt/datasets/tam/backups",
  "api_host": "127.0.0.1", "api_port": 8787 }
```

`TAM_HOME` (or `--home`) overrides the install root; `--db` overrides just the
database file. Pointing `--db` elsewhere on its own still resolves digests and
the API token against the original root — use `--home` to move everything.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

238 tests, no dependencies, roughly twenty-five seconds. They cover the transition
matrix exhaustively (every declared move legal, every undeclared one refused),
both guards, link pairing and cycle detection, every query filter, audit
completeness, concurrent writers against one database, and adapter parity.

## Migrations

Forward-only. Rollback means restoring a snapshot (see [Backups](#backups)),
which for a single-user SQLite database is the honest answer.
