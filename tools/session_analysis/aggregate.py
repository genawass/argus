#!/usr/bin/env python3
"""Merge classifier labels (labels/batch_*.json) with outcomes and print the
failure-mode and success-factor tables."""
import json, sys, glob, collections as C

S = sys.argv[1]
issues = {r["key"]: r for r in map(json.loads, open(f"{S}/issue_table.jsonl"))}
feats = {r["key"]: r for r in map(json.loads, open(f"{S}/issue_features.jsonl"))}
batches = json.load(open(f"{S}/batches.json"))["batches"]
expected = {k for b in batches for k in b}
labels, bad = {}, []
for f in sorted(glob.glob(f"{S}/labels/batch_*.json")):
    try:
        for r in json.load(open(f)):
            labels[r["key"]] = r
    except Exception as e:
        bad.append((f, str(e)[:80]))
print("label files", len(glob.glob(f"{S}/labels/batch_*.json")), "| labelled", len(labels), "of", len(expected), "| unreadable", bad)
print("missing:", sorted(expected - set(labels))[:20])

nonconv = [labels[k] for k in labels if k in issues and issues[k]["outcome"] != "converged_first_pass"]
conv = [labels[k] for k in labels if k in issues and issues[k]["outcome"] == "converged_first_pass"]

print("\n== NON-CONVERGING: primary mode (n=%d) ==" % len(nonconv))
pm = C.Counter(r["primary_mode"] for r in nonconv)
for m, n in pm.most_common():
    art = sum(1 for r in nonconv if r["primary_mode"] == m and r.get("process_artifact"))
    print(f"  {m:26}{n:>4}   (bookkeeping artifact: {art})")
print("any-mention (primary+secondary):")
am = C.Counter(m for r in nonconv for m in [r["primary_mode"], *r.get("secondary_modes", [])])
print("  ", am.most_common())
print("process_artifact overall:", sum(1 for r in nonconv if r.get("process_artifact")), "of", len(nonconv))
print("confidence:", C.Counter(r["confidence"] for r in nonconv))

print("\n== by TAM outcome x mode ==")
for o in ["reworked", "abandoned_open", "cancelled", "in_flight"]:
    sub = [r for r in nonconv if issues[r["key"]]["outcome"] == o]
    print(f"  {o} ({len(sub)}):", C.Counter(r["primary_mode"] for r in sub).most_common(5))

print("\n== CONVERGED SAMPLE: success factors (n=%d) ==" % len(conv))
print("  ", C.Counter(f for r in conv for f in r.get("success_factors", [])).most_common())
print("  hidden struggle:", sum(1 for r in conv if r.get("hidden_struggle")), "of", len(conv))
print("  backfilled_record:", sum(1 for r in conv if "backfilled_record" in r.get("success_factors", [])))
