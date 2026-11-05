# Session analysis

Asks, from the TAM board and the agents' own session transcripts: *which tasks
converged, which did not, and why.* Stdlib Python only; reads local transcripts
and the TAM API, writes derived data to a working directory you choose.

First run: 2026-11-05, 400 issues, 114 sessions. Findings and caveats are in
the report written from it; the short version is that a large share of apparent
failure was status bookkeeping, and no spec-quality or agent hypothesis was
supported by the data.

## Run it

```sh
source /mnt/datasets/tam/env.sh                # TAM_API_URL, TAM_API_TOKEN
export EXCLUDE_SESSION=<id prefix>             # optional: skip the analysing session
tools/session_analysis/run_all.sh ~/session-analysis-work
```

Keep the working directory **outside the repo**. Digests contain issue and
comment text (token-like strings are redacted, long paths too).

`run_all.sh` runs the deterministic stages; the model stage is yours to launch.

| stage | script | writes |
|---|---|---|
| snapshot the board | `fetch_tam.py` | `events.json`, `issues_full.json` |
| Claude sessions | `extract_sessions.py` | `sessions.jsonl` |
| Codex sessions | `extract_codex.py` | `codex_sessions.jsonl` |
| per-session event timelines | `timelines.py` | `timelines.jsonl` |
| outcome class per issue, coverage | `outcomes.py` | `issue_table.jsonl` |
| session activity per issue | `attribute.py` | `issue_features.jsonl` |
| digests for the classifiers | `build_digests.py` | `digests/`, `batches.json` |
| *classify (model)* | `prompts/*.md` | `labels/batch_N.json` |
| tables from the labels | `aggregate.py` | stdout |
| hypothesis tests | `hyp.py` | stdout |
| prompt counts onto the board | `publish_prompt_counts.py` | `human_prompts` metric (dry run unless `--apply`) |

`publish_prompt_counts.py` is the only script that writes to TAM. It skips
containers, and re-running updates values in place. The daily digest then lists
open issues at five or more prompts.

### The classification stage

One subagent per batch in `batches.json`. Batches `0..k` are the non-converging
issues (use `prompts/classify_nonconverging.md`); the rest are a converged
sample (`prompts/label_converged.md`). Substitute `{WORK}` and `{N}` in the
prompt. Then **hand-check about 20 labels against their digests before
trusting any count**: on the first run 12 of 18 held up as stated, and the
"hidden struggle" flag was about half false until the prompt was tightened.

## Outcome definitions

From the TAM status history: `converged_first_pass` (reached review or done
with no move back), `reworked` (review to in_progress, reopened, or
in_progress to todo/backlog), `abandoned_open`, `in_flight`, `cancelled`,
`never_started`, plus `negative_result` for issues labelled `result/negative`
(a sound run whose hypothesis failed; see `docs/CONVENTIONS.md`). "Real" non-convergence in `hyp.py` excludes issues the
classifiers labelled as bookkeeping artifacts, genuine negative results, or
insufficient evidence.

## Things that will bite you

- **TAM timestamps are not work time.** Agents often do the work and record the
  issue afterwards (42% of issues went claim to review in under 15 minutes), so
  session activity can only be attributed to issues claimed *before* the work.
- **Retention.** Claude transcripts older than the local retention window are
  gone, and transcripts from other machines are invisible. `outcomes.py`
  prints how many started issues have a linked session; read that first.
- **Subagents are not sessions.** Claude project directories hold the main
  session plus nested subagent and workflow files; `extract_sessions.py` folds
  them into their parent.
- **Codex command failures** are read from an exit-code field whose name was
  never verified; treat those counts as unvalidated.
- **Statistics are exploratory.** `hyp.py` uses permutation p-values with no
  multiple-comparison correction and a small sample. A feature that wins under
  one definition may vanish under another; the script prints that robustness
  check on purpose.
- Nothing here writes to TAM. `fetch_tam.py` and `build_digests.py` only GET.
