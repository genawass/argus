# Adding a tool

TAM's core contains no knowledge of Slurm, GCP, Encord or anything else. A
watch names a **provider**; the provider observes something and answers in one
canonical vocabulary:

```
running · pending · succeeded · failed · stopped · present · missing ·
unreachable · unknown
```

Everything downstream — the digest's "needs attention" list, alerts, queries —
works off those words, so no tool's vocabulary leaks into the core.

There are two ways to add a tool, and **most need no code at all.**

---

## 1. Configuration only

Use the generic `command` or `http` providers. Between them they cover anything
with a CLI or a REST endpoint.

### A CLI-driven service (GCP Vertex AI shown; adjust for your setup)

```sh
tam watch add TAM-4 command \
  'gcloud ai custom-jobs describe JOB_ID --region=us-central1 --format=json' \
  --config '{
    "parser": "json",
    "paths":  {"loss": "trainingMetrics.loss"},
    "state_patterns": {
      "running":   "JOB_STATE_RUNNING",
      "succeeded": "JOB_STATE_SUCCEEDED",
      "failed":    "JOB_STATE_(FAILED|CANCELLED|EXPIRED)",
      "pending":   "JOB_STATE_(QUEUED|PENDING)"
    }
  }'
```

`state_patterns` maps regexes over the command's output to canonical states.
Without it, exit code 0 means `succeeded` and anything else `failed`.

### A REST API (Encord shown; fill in the real endpoint and fields)

```sh
export ENCORD_TOKEN=...        # never stored in the database

tam watch add TAM-10 http 'https://api.encord.com/v1/<your-endpoint>' \
  --config '{
    "token_env": "ENCORD_TOKEN",
    "state_path": "status",
    "state_map": {"IN_PROGRESS": "running", "COMPLETE": "succeeded",
                  "ERROR": "failed"},
    "parser": "json",
    "paths":  {"labelled": "stats.labelled", "reviewed": "stats.reviewed"},
    "step_path": "stats.batch"
  }'
```

Credentials are named by environment variable (`token_env`), never written to
the database or the config file.

### Other useful shapes

```sh
# anything reachable by shell, on any host
tam watch add TAM-9 command 'docker inspect -f {{.State.Status}} trainer' \
  --host worker-01 --config '{"state_from": "output"}'

# a bucket or directory that should be filling up
tam watch add TAM-3 command 'gsutil ls gs://bucket/prefix | wc -l' \
  --config '{"parser": "regex", "patterns": {"objects": "^(\\d+)$"}}'
```

---

## 2. A provider in code

Worth it when a tool needs real logic — pagination, auth exchange, several
calls. Drop a `.py` file into `$TAM_HOME/data/providers/`; it is picked up
everywhere (CLI, API, MCP, the scan timer) with no change to TAM.

See [`example_custom.py`](example_custom.py) for a working template.

```sh
cp examples/providers/example_custom.py /mnt/datasets/tam/data/providers/
tam provider list          # it appears here
```

Rules for a well-behaved provider:

- **Return, don't raise.** An unreachable host is an `Observation`, not an
  exception. The scanner catches exceptions and records `unknown`, but a
  provider that reports properly gives a far better message.
- **Map into the canonical states.** Keep your tool's own word in
  `native_state` — it is preserved and shown, but nothing branches on it.
- **Read secrets from the environment**, never from the watch config.
- **Be quick.** The scanner runs every 15 minutes across every watch; use the
  timeouts in `tam.shell`.
