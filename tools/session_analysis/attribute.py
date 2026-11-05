#!/usr/bin/env python3
"""Attribute session activity to issues -> issue_features.jsonl.

Window = first claim to the last move into review/cancelled/todo (the final
review->done sign-off is ignored). Active time sums gaps under 30 minutes.
EXCLUDE_SESSION skips the analysing session."""
import json, os, sys, collections as C, statistics as st
from datetime import datetime, timezone

S = sys.argv[1]
SELF = os.environ.get("EXCLUDE_SESSION") or "\0"
NOW = datetime.now(timezone.utc)
GAP = 30 * 60


def T(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


ev_all = json.load(open(f"{S}/events.json"))["data"]
st_ev = C.defaultdict(list)
for e in ev_all:
    if e["field"] == "status":
        st_ev[e["issue"]].append(e)
for v in st_ev.values():
    v.sort(key=lambda e: (e["at"], e["id"]))

issues = [json.loads(l) for l in open(f"{S}/issue_table.jsonl")]
tl = [json.loads(l) for l in open(f"{S}/timelines.jsonl") if not json.loads(l)["session"].startswith(SELF)]
# index: key -> sessions that reference it (any time)
by_key = C.defaultdict(list)
for s in tl:
    ks = set(e[2] for e in s["ev"] if e[1] == "k")
    for k in ks:
        by_key[k].append(s)

rows = []
for i in issues:
    if i["outcome"] == "never_started" or not i["claim_at"]:
        continue
    se = st_ev.get(i["key"], [])
    start = T(se[[e["new"] for e in se].index("in_progress")]["at"])
    if i["status"] in ("in_progress", "blocked"):
        end = NOW
    else:
        ends = [T(e["at"]) for e in se if e["new"] in ("review", "cancelled", "todo", "backlog")]
        end = max(ends) if ends else (T(se[-1]["at"]) if se else NOW)
    sess = by_key.get(i["key"], [])
    f = C.Counter()
    times, other = [], set()
    src = set()
    used = 0
    for s in sess:
        inwin = [e for e in s["ev"] if start <= T(e[0]) <= end]
        if not any(e[1] == "k" and e[2] == i["key"] for e in inwin):
            continue  # session references the key only outside the issue's working window
        used += 1
        src.add(s["source"])
        for ts_, code, n in inwin:
            if code == "k":
                if n != i["key"]:
                    other.add(n)
                continue
            f[code] += n
            times.append(T(ts_))
    times.sort()
    active = sum(min((b - a).total_seconds(), 0) if False else (b - a).total_seconds()
                 for a, b in zip(times, times[1:]) if (b - a).total_seconds() <= GAP)
    rows.append(dict(key=i["key"], outcome=i["outcome"], covered=used > 0, n_sessions=used,
                     sources=sorted(src), window_h=(end - start).total_seconds() / 3600,
                     active_h=active / 3600, prompts=f["p"], interrupts=f["i"], tools=f["t"], tool_err=f["e"],
                     compactions=f["c"], out_tokens=f["o"], concurrent_keys=len(other),
                     labels=i["labels"], priority=i["priority"], bounce=i["bounce"], abandon=i["abandon"],
                     top_actor=i["top_actor"], title=i["title"], parent=i["parent"], reporter=i["reporter"]))

with open(f"{S}/issue_features.jsonl", "w") as o:
    for r in rows:
        o.write(json.dumps(r) + "\n")

cov = [r for r in rows if r["covered"]]
print("started issues", len(rows), "| with in-window session activity", len(cov))
print("by outcome (covered):", C.Counter(r["outcome"] for r in cov))
print()
hdr = f"{'outcome':22}{'n':>4}{'act h':>8}{'tools':>7}{'prompts':>8}{'intr':>6}{'err%':>6}{'compact':>8}{'out k':>7}{'concur':>7}{'sess':>5}"
print(hdr)
for o in ["converged_first_pass", "reworked", "abandoned_open", "in_flight", "cancelled"]:
    v = [r for r in cov if r["outcome"] == o]
    if not v:
        continue
    med = lambda f: st.median(r[f] for r in v)
    errp = st.median((r["tool_err"] / r["tools"] * 100) if r["tools"] else 0 for r in v)
    print(f"{o:22}{len(v):>4}{med('active_h'):>8.1f}{med('tools'):>7.0f}{med('prompts'):>8.0f}{med('interrupts'):>6.0f}"
          f"{errp:>6.1f}{med('compactions'):>8.0f}{med('out_tokens')/1000:>7.0f}{med('concurrent_keys'):>7.0f}{med('n_sessions'):>5.0f}")
