#!/usr/bin/env python3
"""Codex rollouts -> codex_sessions.jsonl (one record per root session).
Derived features only; no transcript text is stored."""
import json, re, sys, glob, os, collections as C
from datetime import datetime

KEY = re.compile(r"\bTAM-\d+\b")
ROOT = os.path.expanduser("~/.codex/sessions")
OUT = os.path.join(sys.argv[1], "codex_sessions.jsonl")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse(path):
    r = dict(file=path, first=None, last=None, human_prompts=0, aborts=0, compactions=0,
             tool_calls=C.Counter(), cmds=0, cmd_fail=0, file_changes=0, keys_tool=C.Counter(),
             keys_text=C.Counter(), tam_http=0, tokens=dict(input=0, cached=0, output=0, reasoning=0),
             guard=C.Counter(), spawns=0, turns=0, repeat_cmds=0, model=None)
    meta = None
    seen = C.Counter()
    last_tok = None
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("timestamp"):
            r["first"] = r["first"] or d["timestamp"]
            r["last"] = d["timestamp"]
        p = d.get("payload")
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if d["type"] == "session_meta":
            meta = p
        elif d["type"] == "turn_context":
            r["model"] = p.get("model") or r["model"]
        elif d["type"] == "event_msg":
            if t == "task_started":
                r["turns"] += 1
            elif t == "turn_aborted":
                r["aborts"] += 1
            elif t == "token_count" and p.get("info"):
                last_tok = p["info"].get("total_token_usage")
            elif t == "task_complete" and "guardian" in str(meta and meta.get("source")):
                try:
                    r["guard"][json.loads(p["last_agent_message"]).get("outcome", "?")] += 1
                except Exception:
                    r["guard"]["?"] += 1
            elif t == "item_completed":
                it = p.get("item", {})
                it_t = it.get("type")
                if it_t == "UserMessage":
                    txt = " ".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
                    if txt.strip():
                        r["human_prompts"] += 1
                        for k in KEY.findall(txt):
                            r["keys_text"][k] += 1
                elif it_t == "ContextCompaction":
                    r["compactions"] += 1
                elif it_t == "FileChange":
                    r["file_changes"] += 1
                elif it_t == "CommandExecution":
                    r["cmds"] += 1
                    code = next((v for k, v in it.items() if "exit" in k.lower() and isinstance(v, int)), None)
                    if code not in (None, 0):
                        r["cmd_fail"] += 1
        elif d["type"] == "response_item" and t in ("custom_tool_call", "function_call"):
            name = p.get("name", "?")
            r["tool_calls"][name] += 1
            if name == "spawn_agent":
                r["spawns"] += 1
            body = str(p.get("input") or p.get("arguments") or "")
            for k in KEY.findall(body):
                r["keys_tool"][k] += 1
            if "/api/issues" in body or "tamapi" in body:
                r["tam_http"] += 1
            if name == "exec":
                seen[re.sub(r"\s+", " ", body)[:200]] += 1
    r["repeat_cmds"] = sum(n - 1 for n in seen.values() if n > 1)
    if last_tok:
        r["tokens"] = dict(input=last_tok.get("input_tokens", 0), cached=last_tok.get("cached_input_tokens", 0),
                           output=last_tok.get("output_tokens", 0), reasoning=last_tok.get("reasoning_output_tokens", 0))
    r["meta"] = dict(id=meta.get("id"), session_id=meta.get("session_id"), parent=meta.get("parent_thread_id"),
                     cwd=meta.get("cwd"), source=str(meta.get("source"))[:80],
                     cli=meta.get("cli_version")) if meta else {}
    return r


mains, kids = {}, C.defaultdict(list)
for p in sorted(glob.glob(f"{ROOT}/**/*.jsonl", recursive=True)):
    rec = parse(p)
    m = rec["meta"]
    if not m:
        continue
    root = m.get("session_id") or m.get("id")
    is_kid = bool(m.get("parent")) and m.get("parent") != m.get("id")
    (kids[root].append(rec) if is_kid else mains.__setitem__(m["id"], rec))

with open(OUT, "w") as out:
    orphan = 0
    for sid, rec in mains.items():
        ks = kids.get(sid, [])
        guard = C.Counter()
        subs = [k for k in ks if "guardian" not in k["meta"]["source"]]
        for k in ks:
            guard.update(k["guard"])
        kt = C.Counter(rec["keys_tool"])
        for k in subs:
            kt.update(k["keys_tool"])
        rec["guard"] = dict(guard)
        rec["n_subagents"] = len(subs)
        rec["sub_tool_calls"] = sum(sum(k["tool_calls"].values()) for k in subs)
        rec["sub_cmd_fail"] = sum(k["cmd_fail"] for k in subs)
        rec["keys_tool_all"] = dict(kt)
        rec["session"] = sid
        rec["wall_s"] = (ts(rec["last"]) - ts(rec["first"])).total_seconds() if rec["first"] else None
        for k in ("tool_calls", "keys_tool", "keys_text"):
            rec[k] = dict(rec[k])
        out.write(json.dumps(rec) + "\n")
    orphan = len([r for r in kids if r not in mains])
    print("codex main", len(mains), "child files", sum(len(v) for v in kids.values()), "orphan groups", orphan)
