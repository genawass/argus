---
name: tam-review
description: Run the TAM daily review — present the 08:00 digest, take the user's decisions in plain language, apply them to the issue database, and close the review out. Use when the user says "daily review", "morning sync", "/tam-review", "what's on today", or asks what needs attention in TAM.
---

# TAM daily review

The 08:00 checkpoint. The digest is already built by a systemd timer at 07:55;
your job is to present it, turn the user's replies into database changes, and
record that the review happened.

`TAM` here refers to `bin/tam` at the repository root. All commands below assume
that path.

## 1. Show the digest

```
./bin/tam review
```

If it reports the digest is already reviewed, say so and ask whether they want
to review again rather than silently re-running it.

Present it **in your own words, not as a raw dump**. Lead with what actually
needs a decision today, in this order:

1. **Overdue** — these are already late; each needs a decision.
2. **Due today**
3. **Blocked** — call out anything blocked with no open blocker; that usually
   means the block is stale and the issue can move.
4. **Stale** — open, untouched for a week. Often the real problem.
5. **Triage queue** — never prioritised or dated.

Skip empty sections silently. If the WIP limit is exceeded, say so plainly:
too much in flight is the thing most likely to be causing the rest.

Keep it short. The user is reading this at 8am; a wall of text defeats the
purpose. Then ask what they want to change.

## 2. Apply their decisions

Interpret plain language and map it onto commands. Common shapes:

| They say | You run |
|---|---|
| "close 13" / "13 is done" | `./bin/tam issue move TAM-13 done` |
| "push 27 to Friday" | `./bin/tam issue update TAM-27 -d 2026-08-14` |
| "drop 31" | `./bin/tam issue move TAM-31 cancelled` |
| "31 is waiting on the vendor" | `./bin/tam issue move TAM-31 blocked --reason "waiting on vendor"` |
| "make 42 urgent" | `./bin/tam issue update TAM-42 -p p0` |
| "start 42" | `./bin/tam issue move TAM-42 in_progress` |
| "9 is blocked by 13" | `./bin/tam link add TAM-9 --blocked-by TAM-13` |
| "add a task to renew certs" | `./bin/tam issue create -t "Renew certs" -p p1 -d +7d` |

Rules:

- **Always pass `--actor daily-review`** so the morning's changes are
  distinguishable from the agent's autonomous ones in the audit log.
- Relative dates work directly: `today`, `tomorrow`, `+3d`.
- Use `--json` when you need to read a result back; the human table is not a
  parsing target.
- If a transition is refused, **report the refusal and the reason — do not
  reach for `--force`.** The guards exist to surface exactly this. Closing a
  parent with open subtasks, or blocking without a cause, is a decision for the
  user to make explicitly.
- If a reference is ambiguous ("close the auth one" with two auth issues), ask
  rather than guess. A wrong close is expensive to notice later.
- Batch related changes, then confirm what you did in one short summary.

## 3. Close it out

Once they are done, record the review with a one-line summary of what changed:

```
./bin/tam review --done "closed 13 and 27, deferred 31 to Friday, 42 to p0"
```

Then confirm in one sentence, and note anything you deliberately left alone.

## Notes

- Every command here also exists as an HTTP endpoint and an MCP tool; if the
  `tam` MCP server is connected, prefer its tools over Bash — same behaviour,
  less quoting.
- If `./bin/tam review` shows a digest with no `generated_at` for today, the
  timer did not run. Check `systemctl --user list-timers tam-digest.timer` and
  mention it — a silently dead timer is the main failure mode of this setup.
