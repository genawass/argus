#!/usr/bin/env python3
"""Per-issue outcome class + task features from the TAM event history,
joined to Claude sessions. Writes issue_table.jsonl and prints coverage."""
import json, os, sys, collections as C
from datetime import datetime

S = sys.argv[1]
ev = json.load(open(f"{S}/events.json"))["data"]
iss = json.load(open(f"{S}/issues_full.json"))
iss = iss.get("data", iss)
iss = iss.get("issues", iss) if isinstance(iss, dict) else iss
SELF = os.environ.get("EXCLUDE_SESSION") or "\0"  # the analysing session references keys it only talks about
sessions = [s for s in map(json.loads, open(f"{S}/sessions.jsonl")) if not s["session"].startswith(SELF)]


def T(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


by = C.defaultdict(list)
for e in ev:
    by[e["issue"]].append(e)

sess_by_key = C.defaultdict(list)
for s in sessions:
    for k, n in s["keys_tool_all"].items():
        sess_by_key[k].append((s["session"], n))

rows = []
for i in iss:
    k = i["key"]
    es = sorted(by.get(k, []), key=lambda e: (e["at"], e["id"]))
    st = [e for e in es if e["field"] == "status"]
    path = [e["new"] for e in st]
    claim = next((e for e in st if e["new"] == "in_progress"), None)
    review = next((e for e in st if e["new"] in ("review", "done")), None)
    bounce = sum(1 for e in st if (e["old"], e["new"]) in {("review", "in_progress"), ("done", "in_progress"), ("done", "todo"), ("review", "todo")})
    abandon = sum(1 for e in st if e["old"] == "in_progress" and e["new"] in ("todo", "backlog"))
    blocked = sum(1 for e in st if e["new"] == "blocked")
    cur = i["status"]
    reached = review is not None
    negative = "result/negative" in (i.get("labels") or [])   # tam.models.NEGATIVE_LABEL
    if cur == "cancelled":
        outcome = "cancelled"
    elif negative and reached:
        outcome = "negative_result"      # ran correctly, hypothesis failed: a result, not rework
    elif reached and bounce == 0 and abandon == 0:
        outcome = "converged_first_pass"
    elif reached:
        outcome = "reworked"
    elif cur in ("in_progress", "blocked"):
        outcome = "in_flight"
    elif claim:
        outcome = "abandoned_open"
    else:
        outcome = "never_started"
    ttr = (T(review["at"]) - T(claim["at"])).total_seconds() / 3600 if claim and review else None
    actors = C.Counter(e["actor"] for e in es if e["actor"] not in ("scanner", "slurm-scan"))
    rows.append(dict(
        key=k, title=i["title"], type=i.get("type"), priority=i.get("priority"), status=cur,
        outcome=outcome, bounce=bounce, abandon=abandon, blocked=blocked, ttr_h=ttr,
        comments=sum(1 for e in es if e["kind"] == "commented"),
        n_events=len(es), reporter=i.get("reporter"), assignee=i.get("assignee"),
        parent=i.get("parent"), labels=i.get("labels"), created=i.get("created_at"),
        top_actor=actors.most_common(1)[0][0] if actors else None,
        claim_at=claim["at"] if claim else None, review_at=review["at"] if review else None,
        sessions=[s for s, _ in sess_by_key.get(k, [])],
        session_calls=sum(n for _, n in sess_by_key.get(k, [])),
    ))

with open(f"{S}/issue_table.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")

print("issues", len(rows))
tab = C.Counter((r["outcome"], bool(r["sessions"])) for r in rows)
print(f"{'outcome':24}{'total':>7}{'w/ claude session':>20}")
for o in ["converged_first_pass", "negative_result", "reworked", "abandoned_open", "in_flight", "cancelled", "never_started"]:
    tot = tab[(o, True)] + tab[(o, False)]
    print(f"{o:24}{tot:>7}{tab[(o, True)]:>20}")
started = [r for r in rows if r["outcome"] not in ("never_started",)]
print("started issues:", len(started), "covered by claude:", sum(1 for r in started if r["sessions"]))
print("\nstarted & uncovered, by top actor:")
print(C.Counter(r["top_actor"] for r in started if not r["sessions"]).most_common(12))
print("started & covered, by top actor:")
print(C.Counter(r["top_actor"] for r in started if r["sessions"]).most_common(12))
