#!/usr/bin/env python3
"""Write digests/<KEY>.md for every non-converging issue plus a matched sample of
40 converged ones, and batches.json (8 issues per batch). Redacts token-like
strings. Needs TAM_API_URL / TAM_API_TOKEN (GET only); EXCLUDE_SESSION skips
the analysing session."""
import json, os, re, sys, glob, random, urllib.request, collections as C
from datetime import datetime

S = sys.argv[1]
API = os.environ["TAM_API_URL"]
TOK = os.environ["TAM_API_TOKEN"]
CROOT = os.path.expanduser("~/.claude/projects")
SELF = os.environ.get("EXCLUDE_SESSION") or "\0"
random.seed(7)


def T(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


RED = [(re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1[REDACTED]"),
       (re.compile(r"\b(sk|ghp|gho|xox[bap]|AKIA|hf)[-_A-Za-z0-9]{16,}"), "[REDACTED]"),
       (re.compile(r"(?i)(token|secret|password|api[_-]?key)(\s*[=:]\s*)\S+"), r"\1\2[REDACTED]"),
       (re.compile(r"\b[A-Za-z0-9+/_\-]{40,}\b"), "[REDACTED]")]


def red(s):
    for p, r in RED:
        s = p.sub(r, s)
    return s


def clip(s, n):
    s = red(re.sub(r"\s+", " ", s or "").strip())
    return s if len(s) <= n else s[:n] + " …"


def api(path):
    rq = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOK}"})
    return json.load(urllib.request.urlopen(rq, timeout=30))["data"]


issues = {r["key"]: r for r in map(json.loads, open(f"{S}/issue_table.jsonl"))}
feats = {r["key"]: r for r in map(json.loads, open(f"{S}/issue_features.jsonl"))}
nonconv = [k for k, r in issues.items() if r["outcome"] in ("reworked", "abandoned_open", "cancelled", "in_flight")]
conv = [k for k, r in issues.items() if r["outcome"] == "converged_first_pass"]
# matched sample: prefer ones with session coverage, spread over top_actor
random.shuffle(conv)
conv.sort(key=lambda k: (k not in feats or not feats[k]["covered"]))
pick, per = [], C.Counter()
for k in conv:
    a = issues[k]["top_actor"]
    if per[a] < 4:
        pick.append(k)
        per[a] += 1
    if len(pick) == 40:
        break

# session index
cl_path = {}
for p in glob.glob(f"{CROOT}/*/*.jsonl"):
    cl_path[os.path.basename(p)[:-6]] = p
cx_path = {r["session"]: r["file"] for r in map(json.loads, open(f"{S}/codex_sessions.jsonl"))}
tl = {r["session"]: r for r in map(json.loads, open(f"{S}/timelines.jsonl"))}
key_sessions = C.defaultdict(list)
for sid, r in tl.items():
    if sid.startswith(SELF):
        continue
    for ks in {e[2] for e in r["ev"] if e[1] == "k"}:
        key_sessions[ks].append((sid, r["source"]))


def prompts(sid, source, lo, hi):
    out = []
    if source == "claude" and sid in cl_path:
        for l in open(cl_path[sid], errors="replace"):
            try:
                d = json.loads(l)
            except Exception:
                continue
            if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta") or not d.get("timestamp"):
                continue
            t = T(d["timestamp"])
            if not lo <= t <= hi:
                continue
            c = d.get("message", {}).get("content")
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                continue
            txt = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
            txt = txt.strip()
            if txt and not txt.startswith(("<local-command", "<command-name", "<system-reminder")):
                out.append((d["timestamp"], txt))
    elif source == "codex" and sid in cx_path:
        for l in open(cx_path[sid], errors="replace"):
            try:
                d = json.loads(l)
            except Exception:
                continue
            p = d.get("payload")
            if not isinstance(p, dict) or p.get("type") != "item_completed":
                continue
            it = p.get("item", {})
            if it.get("type") != "UserMessage":
                continue
            t = T(d["timestamp"])
            if lo <= t <= hi:
                txt = " ".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict)).strip()
                if txt:
                    out.append((d["timestamp"], txt))
    return out


os.makedirs(f"{S}/digests", exist_ok=True)
allk = nonconv + pick
for k in allk:
    r = issues[k]
    full = api(f"/api/issues/{k}?comments=1&history=1")
    h = []
    h.append(f"# {k} — {red(r['title'])}")
    h.append(f"outcome(by TAM status path): {r['outcome']} | priority {r['priority']} | type {r['type']} | labels {r['labels']} | parent {r['parent']} | reporter {r['reporter']} | main actor {r['top_actor']}")
    h.append(f"bounce-backs {r['bounce']} | abandon moves {r['abandon']} | blocked moves {r['blocked']} | claim→review hours {None if r['ttr_h'] is None else round(r['ttr_h'], 2)}")
    h.append("\n## Body\n" + clip(full.get("body"), 1500))
    hist = full.get("history") or []
    sts = [e for e in hist if e.get("field") == "status"]
    h.append("\n## Status path\n" + "\n".join(f"- {e['at']} {e['actor']}: {e['old']}→{e['new']}" + (f" ({clip(e['note'],160)})" if e.get("note") else "") for e in sts[-25:]))
    cm = full.get("comments") or []
    h.append(f"\n## Comments ({len(cm)}; showing up to 14, last 14 if more)")
    for c in cm[-14:]:
        h.append(f"- [{c.get('created_at') or c.get('at')}] {c.get('author')}: {clip(c.get('body'), 700)}")
    f = feats.get(k)
    if f and f["covered"]:
        h.append(f"\n## Session activity inside the working window\nactive {f['active_h']:.1f}h, {f['tools']} tool calls, {f['prompts']} human prompts, {f['interrupts']} interrupts, {f['tool_err']} tool errors, {f['n_sessions']} session(s), sources {f['sources']}")
        sel = se = None
        evs = [e for e in hist if e.get("field") == "status"]
        claim = next((e for e in evs if e["new"] == "in_progress"), None)
        lo = T(claim["at"]) if claim else None
        endc = [T(e["at"]) for e in evs if e["new"] in ("review", "cancelled", "todo", "backlog")]
        hi = max(endc) if endc else datetime.max.replace(tzinfo=lo.tzinfo if lo else None)
        if lo:
            ps = []
            for sid, src in key_sessions.get(k, []):
                ps += prompts(sid, src, lo, hi)
            ps.sort()
            h.append(f"human prompts in window ({len(ps)}; first 8 shown):")
            for ts_, txt in ps[:8]:
                h.append(f"- [{ts_}] {clip(txt, 350)}")
    else:
        h.append("\n## Session activity\nnone attributable (no linked transcript, or work happened before the claim)")
    open(f"{S}/digests/{k}.md", "w").write("\n".join(h) + "\n")

random.shuffle(nonconv)
batches = [nonconv[i:i + 8] for i in range(0, len(nonconv), 8)] + [pick[i:i + 8] for i in range(0, len(pick), 8)]
json.dump(dict(batches=batches, nonconv=len(nonconv), sample=len(pick)), open(f"{S}/batches.json", "w"))
print("digests", len(allk), "nonconv", len(nonconv), "sample", len(pick), "batches", len(batches))
