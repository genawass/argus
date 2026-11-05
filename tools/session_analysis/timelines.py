#!/usr/bin/env python3
"""Timelines: per root session, a list of [ts, code, n] events and [ts, 'k', key] key references.
codes: p human prompt, i interrupt/abort, t tool call, e tool error, c compaction, o output tokens(n).
Timestamps and counts only."""
import json, re, os, glob, sys, collections as C

KEY = re.compile(r"\bTAM-\d+\b")
S = sys.argv[1]
CROOT = os.path.expanduser("~/.claude/projects")
XROOT = os.path.expanduser("~/.codex/sessions")


def claude_events(path):
    ev = []
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        t, ts = d.get("type"), d.get("timestamp")
        if not ts:
            continue
        if t == "assistant":
            m = d.get("message", {})
            n = (m.get("usage") or {}).get("output_tokens") or 0
            if n:
                ev.append([ts, "o", n])
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    ev.append([ts, "t", 1])
                    for k in set(KEY.findall(json.dumps(b.get("input", {})))):
                        ev.append([ts, "k", k])
        elif t == "user":
            c = d.get("message", {}).get("content")
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                        ev.append([ts, "e", 1])
            elif not d.get("isSidechain") and not d.get("isMeta"):
                txt = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
                txt = txt.strip()
                if txt and not txt.startswith(("<local-command", "<command-name")):
                    ev.append([ts, "i" if "[Request interrupted" in txt else "p", 1])
        elif t == "system" and "compact" in (str(d.get("subtype", "")) + str(d.get("content", ""))[:200]).lower():
            ev.append([ts, "c", 1])
    return ev


def codex_events(path):
    ev, meta, prev = [], None, 0
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts, p = d.get("timestamp"), d.get("payload")
        if not ts or not isinstance(p, dict):
            continue
        t = p.get("type")
        if d["type"] == "session_meta":
            meta = p
        elif "guardian" in str(meta and meta.get("source")):
            continue
        elif d["type"] == "response_item" and t in ("custom_tool_call", "function_call"):
            ev.append([ts, "t", 1])
            for k in set(KEY.findall(str(p.get("input") or p.get("arguments") or ""))):
                ev.append([ts, "k", k])
        elif d["type"] == "event_msg":
            if t == "turn_aborted":
                ev.append([ts, "i", 1])
            elif t == "token_count" and p.get("info"):
                n = (p["info"].get("last_token_usage") or {}).get("output_tokens") or 0
                if n:
                    ev.append([ts, "o", n])
            elif t == "item_completed":
                it = p.get("item", {})
                if it.get("type") == "UserMessage" and any(c.get("text", "").strip() for c in it.get("content", []) if isinstance(c, dict)):
                    ev.append([ts, "p", 1])
                elif it.get("type") == "ContextCompaction":
                    ev.append([ts, "c", 1])
                elif it.get("type") == "CommandExecution":
                    code = next((v for k, v in it.items() if "exit" in k.lower() and isinstance(v, int)), None)
                    if code not in (None, 0):
                        ev.append([ts, "e", 1])
    return ev, meta


out = open(f"{S}/timelines.jsonl", "w")
# Claude
by = C.defaultdict(list)
for p in glob.glob(f"{CROOT}/**/*.jsonl", recursive=True):
    if os.path.basename(p) == "journal.jsonl":
        continue
    rel = os.path.relpath(p, CROOT).split(os.sep)
    sid = rel[1][:-6] if len(rel) == 2 else rel[1]
    by[(rel[0], sid)].extend(claude_events(p))
for (proj, sid), ev in by.items():
    ev.sort()
    out.write(json.dumps(dict(source="claude", session=sid, project=proj, ev=ev)) + "\n")
# Codex
roots = C.defaultdict(list)
for p in glob.glob(f"{XROOT}/**/*.jsonl", recursive=True):
    ev, meta = codex_events(p)
    if not meta:
        continue
    if "guardian" in str(meta.get("source")):
        continue
    kid = bool(meta.get("parent_thread_id")) and meta.get("parent_thread_id") != meta.get("id")
    roots[(meta.get("session_id") or meta.get("id")) if kid else meta.get("id")].extend(ev)
for sid, ev in roots.items():
    ev.sort()
    out.write(json.dumps(dict(source="codex", session=sid, project="codex", ev=ev)) + "\n")
print("timelines claude", len(by), "codex", len(roots))
