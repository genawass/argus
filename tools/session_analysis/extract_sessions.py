#!/usr/bin/env python3
"""Step 1-2: one record per Claude Code session -> sessions.jsonl.

Only derived features are written (counts, timings, issue keys). No transcript
text is stored, because transcripts contain tokens and local paths.
"""
import json, re, sys, glob, os, collections as C
from datetime import datetime

KEY = re.compile(r"\bTAM-\d+\b")
ROOT = os.path.expanduser("~/.claude/projects")
OUT = os.path.join(sys.argv[1], "sessions.jsonl")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def is_human_prompt(d):
    if d.get("isSidechain") or d.get("isMeta"):
        return False
    c = d.get("message", {}).get("content")
    if isinstance(c, str):
        txt = c
    elif isinstance(c, list):
        if any(b.get("type") == "tool_result" for b in c if isinstance(b, dict)):
            return False
        txt = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    else:
        return False
    txt = txt.strip()
    if not txt or txt.startswith("<local-command") or txt.startswith("<command-name"):
        return False
    return True


def text_of(d):
    c = d.get("message", {}).get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


def parse(path):
    r = dict(session=os.path.basename(path)[:-6], project=os.path.basename(os.path.dirname(path)),
             first=None, last=None, human_prompts=0, interrupts=0, assistant_turns=0,
             tool_calls=C.Counter(), tool_errors=0, tool_results=0, out_tokens=0, in_tokens=0,
             cache_read=0, models=C.Counter(), cost_usd=None, active_ms=None, compactions=0,
             keys_tool=C.Counter(), keys_text=C.Counter(), tam_writes=C.Counter(),
             repeat_cmds=0, sidechain_turns=0, title=None, first_prompt_len=None)
    seen_cmds = C.Counter()
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type")
        if d.get("timestamp"):
            r["first"] = r["first"] or d["timestamp"]
            r["last"] = d["timestamp"]
        if t == "ai-title":
            r["title"] = d.get("aiTitle")
        elif t == "cost-state":
            r["cost_usd"] = d.get("totalCostUSD")
            r["active_ms"] = d.get("totalDuration")
        elif t == "system" and "compact" in json.dumps(d.get("subtype", "")) + json.dumps(d.get("content", ""))[:200].lower():
            r["compactions"] += 1
        elif t == "user":
            if is_human_prompt(d):
                txt = text_of(d)
                r["human_prompts"] += 1
                if r["first_prompt_len"] is None:
                    r["first_prompt_len"] = len(txt)
                if "[Request interrupted" in txt:
                    r["interrupts"] += 1
                else:
                    for k in KEY.findall(txt):
                        r["keys_text"][k] += 1
            c = d.get("message", {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        r["tool_results"] += 1
                        if b.get("is_error"):
                            r["tool_errors"] += 1
        elif t == "assistant":
            if d.get("isSidechain"):
                r["sidechain_turns"] += 1
            r["assistant_turns"] += 1
            m = d.get("message", {})
            u = m.get("usage") or {}
            r["out_tokens"] += u.get("output_tokens") or 0
            r["in_tokens"] += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            r["cache_read"] += u.get("cache_read_input_tokens") or 0
            if m.get("model"):
                r["models"][m["model"]] += 1
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    name = b.get("name", "?")
                    r["tool_calls"][name] += 1
                    inp = json.dumps(b.get("input", {}))
                    for k in KEY.findall(inp):
                        r["keys_tool"][k] += 1
                    if name.startswith("mcp__tam__"):
                        r["tam_writes"][name[len("mcp__tam__"):]] += 1
                    elif name == "Bash":
                        cmd = b.get("input", {}).get("command", "")
                        if "/api/issues" in cmd or "tamapi" in cmd:
                            r["tam_writes"]["http:" + ("POST" if "POST" in cmd else "GET")] += 1
                        norm = re.sub(r"\s+", " ", cmd)[:200]
                        seen_cmds[norm] += 1
    r["repeat_cmds"] = sum(n - 1 for n in seen_cmds.values() if n > 1)
    if r["first"] and r["last"]:
        r["wall_s"] = (ts(r["last"]) - ts(r["first"])).total_seconds()
    return r



def owner(path):
    rel = os.path.relpath(path, ROOT).split(os.sep)
    proj = rel[0]
    if len(rel) == 2:
        return proj, rel[1][:-6], False
    return proj, rel[1], True


mains, subs = {}, C.defaultdict(list)
for p in sorted(glob.glob(f"{ROOT}/**/*.jsonl", recursive=True)):
    proj, sid, is_sub = owner(p)
    if os.path.basename(p) == "journal.jsonl":
        continue
    rec = parse(p)
    (subs[(proj, sid)].append(rec) if is_sub else mains.__setitem__((proj, sid), rec))

with open(OUT, "w") as out:
    for key, rec in mains.items():
        sl = subs.get(key, [])
        rec["n_subagents"] = len(sl)
        rec["sub_out_tokens"] = sum(x["out_tokens"] for x in sl)
        rec["sub_tool_calls"] = sum(sum(x["tool_calls"].values()) for x in sl)
        rec["sub_tool_errors"] = sum(x["tool_errors"] for x in sl)
        kt = C.Counter(rec["keys_tool"])
        for x in sl:
            kt.update(x["keys_tool"])
        rec["keys_tool_all"] = dict(kt)
        for k in ("tool_calls", "models", "keys_tool", "keys_text", "tam_writes"):
            rec[k] = dict(rec[k])
        out.write(json.dumps(rec) + "\n")
    print("main sessions", len(mains), "subagent files", sum(len(v) for v in subs.values()),
          "orphan subagent groups", len([k for k in subs if k not in mains]))
