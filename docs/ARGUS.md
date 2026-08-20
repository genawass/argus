# ARGUS — instructions for agents

*Argus Panoptes, the hundred-eyed watchman, never slept with every eye at once.
The scanner is the same: it probes every watched job on a timer and reports what
changed. Your part is to give it eyes — bind a watch to whatever you launch, and
record what you find.*

Reference this file from `CLAUDE.md` rather than copying it. The live version is
served by the API that enforces the rules it describes, so it cannot drift from
them:

```sh
curl -H "Authorization: Bearer $TAM_API_TOKEN" "$TAM_API_URL/docs/ARGUS.md"
curl -H "Authorization: Bearer $TAM_API_TOKEN" "$TAM_API_URL/docs"   # what else
```

TAM is the shared task database. **Every agent records its own work there.** That
is what lets one person see, in one place, what is running across the cluster
without asking each agent.

---

## Setup

Three environment variables. Put the first two in `~/.bashrc`, and `TAM_ACTOR`
in the workspace so each agent gets its own identity. If you set nothing, writes are attributed to `agent@<hostname>` — distinguishable, but say who you are if you can:

```jsonc
// .vscode/settings.json  — one actor per repo
{
  "terminal.integrated.env.linux": {
    "TAM_API_URL": "http://192.168.10.184:8787",
    "TAM_ACTOR": "navy-agent"
  }
}
```

```sh
# ~/.bashrc — keep the token out of anything committed to git
source /mnt/datasets/tam/env.sh          # sets TAM_API_URL and TAM_API_TOKEN
```

Check it works:

```sh
curl -s "$TAM_API_URL/health"
```

A convenience wrapper, since every call repeats the same headers:

```sh
tamapi() {           # tamapi GET /api/issues?priority=p1
  local method=$1 path=$2; shift 2
  curl -s -X "$method" -H "Authorization: Bearer $TAM_API_TOKEN" \
       -H 'Content-Type: application/json' "$TAM_API_URL$path" "$@"
}
```

The HTTP API is the whole interface from a worker node. There is no TAM code on
shared storage to source or add to `PATH`: the database is local disk on the
serving host, and every other node is an API client. If you want the `tam` CLI
on your machine, clone the repo and keep `TAM_API_URL` set — the CLI then speaks
to the same API, with the same behaviour and the same rules.

---

## The loop

### 1. Find your work

```sh
tamapi GET '/api/issues?labels=navy&status=todo,in_progress'
```

Filter by whatever scopes you: `labels`, `parent`, `priority`, `assignee`.
`GET /api/issues?priority=p0,p1` is the general "what matters" query.

### 2. Read before acting

```sh
tamapi GET /api/issues/TAM-23
```

Returns the issue with its subtasks, links, allowed next statuses — and prior
comments if you add `?comments=1&history=1`. **Read the history.** Somebody
(often a previous agent) has usually already tried something, and the reason it
failed is recorded there.

### 3. Claim it

```sh
tamapi POST /api/issues/TAM-23/transition \
  -d "{\"status\":\"in_progress\",\"actor\":\"$TAM_ACTOR\"}"
```

Status changes go through `/transition`, never `PATCH`. The workflow rules are
enforced server-side, so an illegal move returns `409` with the legal options.

### 4. Bind a watch to anything you launch

This is the highest-value thing you can do. A watch means the board tracks your
job every 15 minutes without you doing anything further — and a job that dies is
visible instead of silently stalling.

```sh
# a Slurm job, parsing YOLO metrics out of its log
tamapi POST /api/issues/TAM-23/watches -d "{
  \"provider\":\"slurm\", \"ref\":\"38577\",
  \"config\":{\"parser\":\"yolo\",
              \"state_patterns\":{\"stopped\":\"USER ABORTED|TASK STOPPED\"}},
  \"actor\":\"$TAM_ACTOR\"}"

# a process on another host
tamapi POST /api/issues/TAM-23/watches -d "{
  \"provider\":\"process\", \"ref\":\"train.py\", \"host\":\"worker-node04\",
  \"actor\":\"$TAM_ACTOR\"}"

# an output directory that should be growing
tamapi POST /api/issues/TAM-23/watches -d "{
  \"provider\":\"path\", \"ref\":\"/mnt/datasets/out\", \"host\":\"worker-node04\",
  \"actor\":\"$TAM_ACTOR\"}"
```

`GET /api/providers` lists what else is available, including `command` and
`http` which cover any CLI or REST service (GCP, Encord, CI) by configuration.

Include `state_patterns` for anything Slurm-scheduled. Slurm reports `COMPLETED`
whenever the wrapper script exits 0 — including when the training inside it was
killed. Without the pattern, an aborted run reads as success.

### 5. Record numbers, not prose

```sh
tamapi POST /api/issues/TAM-23/metrics \
  -d "{\"name\":\"recall\",\"value\":0.814,\"step\":111,\"source\":\"job-38577\"}"
```

Metrics are queryable and trendable; a comment saying "recall improved" is not.
If the issue carries a target, the metric is what evaluates it:

```sh
tamapi GET '/api/targets?issue=TAM-23'      # met / not met, and by how much
tamapi GET '/api/issues/TAM-23/metrics/trend?name=recall&window=10'
```

The trend endpoint tests the slope against its own scatter, so it will say
`flat` for a noisy series whose endpoints happen to differ. Trust it over
eyeballing first-vs-last.

Run configuration goes in params, so results can be traced back:

```sh
tamapi POST /api/issues/TAM-23/params \
  -d "{\"params\":{\"lr\":0.001,\"batch\":16,\"dataset\":\"navy_v11\"},
       \"source\":\"$TAM_ACTOR\"}"
```

### 6. Comment for judgement, not for data

Use comments for what a number cannot carry: why an approach was abandoned,
what you suspect, what you ruled out.

```sh
tamapi POST /api/issues/TAM-23/comments \
  -d "{\"body\":\"Aborted at ep111 - validation flat across the last 20 evals.\",
       \"author\":\"$TAM_ACTOR\"}"
```

### 7. Close it out

```sh
tamapi POST /api/issues/TAM-23/transition \
  -d "{\"status\":\"review\",\"actor\":\"$TAM_ACTOR\"}"
```

If you are abandoning rather than finishing, move it back to `todo` and say why.

---

## Rules

**Never leave an issue `in_progress` when nothing is running.** This is the one
that matters most. An issue marked in-progress with no live watch is
indistinguishable from work that quietly died — and that has already happened
here, costing seven hours of GPU with nothing to show.

**Use your own `TAM_ACTOR`.** The audit log is only useful if it says who did
what. Never write as a generic `agent`.

**Work only on your own issues.** Agents are assumed to be working on disjoint
tasks. There is no optimistic concurrency: if two agents write to the same
issue, the later write wins silently and the earlier one is lost without an
error.

**Blocking needs a cause.** Moving to `blocked` requires either a `reason` or a
`blocked_by` link. A blocked issue with no stated cause is the most common way a
board rots.

**Do not force a closure to make things look tidy.** Closing a parent with open
subtasks needs `force`, and the fact that it was forced is recorded.

**Report negative results.** An experiment that did not work is information. It
belongs in the issue, with the numbers.

---

## If something looks wrong

```sh
tamapi GET '/api/events?since=2026-08-12T00:00:00Z'   # change feed
tamapi GET /api/issues/TAM-23/history                 # who changed what
tamapi POST /api/scan -d '{"issue":"TAM-23"}'         # probe watches now
```

`GET /api/events?since=` is the cheap way to catch up on what changed while you
were working, rather than re-reading the board.

Full endpoint reference: [`docs/API.md`](docs/API.md) — 42 endpoints, generated
from the route table so it cannot drift.
