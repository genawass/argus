# Board conventions

How the board is meant to be filled in. These are conventions, not constraints:
`core` enforces the workflow matrix, not this file. But a board that ignores
them stops answering the one question it exists to answer — *what should I work
on next?*

---

## 1. The problem this fixes

An audit on 2026-08-13 found **34 of 39 issues typed `task`**, with `spike` used
three times. One type was carrying four different kinds of thing, and because
`in_progress` meant something different for each, the WIP number was measuring
nothing:

| what it really was | examples at the time | when is it done? |
|---|---|---|
| container | TAM-1 (8 children), TAM-2 (15), TAM-3 (3), TAM-4 (8) | never — it rolls up |
| standing goal | TAM-5 "Accuracy above 90", TAM-6 "Error rate below 10" | when a metric crosses |
| planning | TAM-11 "Architecture spec" | when a decision is written |
| real work | TAM-20 retrain, TAM-37 materialize | when an artifact exists |

Eighteen issues were `in_progress` against a `wip_limit` of 3. Six of them —
four containers and two standing goals — were not work anyone could do.

---

## 2. Sort by done-condition, not by subject

**The question that assigns the type is "what has to be true for this to
close?"** Subject matter is irrelevant; two issues about the same model can be
different types.

### `task` — done when an artifact exists

A file, a trained model, a deployed binary, a materialized directory. The
done-condition is checkable by looking at something that did not exist before.

> TAM-41 *Regenerate the label set for the training corpus* — done when the
> labels are on disk.

### `spike` — done when a decision is written

Investigation, evaluation, spec work. The output is knowledge, so it is only
done when the knowledge is **recorded on the issue as a comment**. A spike that
ends in someone's head is not done.

> TAM-23 *Evaluate two candidate training frameworks* — done when the board says
> which one and why.

Because the output is a decision, a spike is where an unknown gets *closed*.
If a spike's answer creates work, that work is a new `task`; do not let the
spike mutate into the implementation.

### `bug` — done when behaviour changes

Something already built does the wrong thing. A diagnosis with no fix is a
`spike`; the `bug` closes when the behaviour is corrected.

### `chore` — done when a recurring obligation is discharged

Backups, upgrades, cleanups. No lasting artifact, no decision.

---

## 3. Goals are targets, not issues

**An outcome expressed as a threshold is a `target`, not an issue.** TAM has
first-class machinery for this — `POST /api/issues/{key}/targets` with a metric,
an operator and a value — and the digest already reports unmet ones under
`targets_unmet`.

The test: *can effort alone close it?* "Train model X" closes when someone
finishes. "Accuracy above 90" closes when a number moves, which may never happen.
The second is an acceptance criterion and belongs on the work it judges.

TAM-5 and TAM-6 were the worked example. Both were issues whose entire content
was a threshold; both read their metric from TAM-1 (`metric_from: TAM-1`) even
before the change, because TAM-1 is where the metric was recorded. They are now
targets on TAM-1 — same metric, same threshold, same note — and TAM-1 reports
`recall >= 0.9` and `precision >= 0.9` directly.

Note the distinction that matters: **work that carries a target stays a task.**
TAM-14 (*Train on the full corpus*, `map50 >= 0.75`) and TAM-32 (*Add an
augmentation stage*, `accuracy >= 0.9`) are real work being *judged* by a
target. Only an issue that is *nothing but* a threshold should be collapsed.

---

## 4. Containers are not work

Nesting is a single level (`core/issues.py::_resolve_parent`), so any issue
named as a parent is a container and any issue with a parent is a leaf.

A container's status is a reflection of its children. It goes `in_progress`
because work started underneath it, and nobody ever sits down to work on it
directly. As of 2026-08-13 the digest reflects this: `wip` counts leaf issues
only, so containers no longer consume the limit. Covered by
`tests/test_digest.py`.

This is also why a container with only closed children still does not count —
it is a grouping, and closing the children does not turn it back into work.

**Keep containers thin.** A container's body should say what the group is for;
put the work in the leaves. If you find yourself commenting progress on a
container, the work probably wants a leaf of its own.

---

## 5. Worked example

A model-delivery project, decomposed by done-condition rather than by subject —
note that every row below is about the same model, and no two rows are the same
type:

| issue | type | done when |
|---|---|---|
| Deliver the model (TAM-18) | container | rolls up |
| Settle the label taxonomy | `spike` | the competing schemes are reconciled in writing |
| Regenerate the label set (TAM-41) | `task` | the labels are on disk |
| Handle oversized inputs in the labeller | `task` | large frames are processed without downscaling |
| Make the labeller robust on the second sensor | `task` + target | a task to build it, a target to judge it |

The last row is the point of the whole scheme. "Make it robust" as a bare task
has no done-condition and would sit `in_progress` for months — exactly the
failure TAM-5 and TAM-6 demonstrated. Split into a task that builds something
and a target that says what "robust" means as a number, and both halves become
answerable.

---

## 6. Quick reference

- Artifact → `task`. Decision → `spike`. Behaviour change → `bug`. Obligation → `chore`.
- A threshold on its own → a **target**, on the issue it judges.
- Has children → container. Don't work it, don't count it.
- A `spike` is done when its answer is **written on the issue**.
- If you cannot say what closes an issue, it is not ready to be `in_progress`.
