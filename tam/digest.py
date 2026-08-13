"""The daily digest: what the 08:00 checkpoint is built on.

Generation is deliberately dependency-free and offline. It runs from a systemd
timer whether or not an agent is available, so the record of "what the board
looked like this morning" never has a gap.
"""

import json
from datetime import date

from .clock import parse_date, shift, today, utcnow
from . import providers
from .core import IssueFilter
from .db import tx

SECTION_TITLES = [
    ("overdue", "Overdue"),
    ("due_today", "Due today"),
    ("due_this_week", "Due this week"),
    ("in_progress", "In progress"),
    ("blocked", "Blocked"),
    ("stale", "Stale"),
    ("triage", "Triage queue"),
    ("closed_recently", "Closed since last digest"),
]


def _days_between(a, b):
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def build(svc, run_date=None):
    """Assemble today's digest payload. Pure read -- writes nothing."""
    tz = svc.config.timezone
    run_date = parse_date(run_date, "date") or today(tz).isoformat()
    week_end = shift(run_date, 7)

    def issues(**kw):
        return svc.list_issues(IssueFilter(**kw))

    overdue = [
        {**i.to_dict(), "days_late": _days_between(run_date, i.due_date)}
        for i in issues(due_before=run_date, sort="due_date")
    ]
    due_today = [i.to_dict() for i in issues(due_on=run_date, sort="priority")]
    due_week = [
        i.to_dict()
        for i in issues(due_after=run_date, sort="due_date")
        if i.due_date <= week_end
    ]

    in_progress = [i.to_dict() for i in issues(status=("in_progress",), sort="priority")]

    blocked = []
    for i in issues(is_blocked=True, sort="priority"):
        blockers = svc.blockers(i.key)
        reason = None
        if not blockers:
            for e in reversed(svc.history(i.key)):
                if e.kind == "transitioned" and e.new_value == "blocked":
                    reason = e.note
                    break
        blocked.append({
            **i.to_dict(),
            "blocked_by": [
                {"key": b.to_key, "title": b.to_title, "status": b.to_status}
                for b in blockers
            ],
            "reason": reason,
        })

    stale = [
        i.to_dict()
        for i in issues(stale_days=svc.config.stale_days, sort="updated_at")
    ]

    # "Triage" means nobody has made a decision about it yet: still in backlog,
    # no due date, priority left at the default.
    triage = [
        i.to_dict()
        for i in issues(status=("backlog",), sort="created_at")
        if not i.due_date and i.priority == "p2"
    ]

    # Targets and watches: what the board is measured against, and what is
    # currently running for it.
    targets = [t for t in svc.evaluate_targets() if t["status"] != "met"]
    watches = svc.list_watches()
    attention = [w for w in watches if (w["state"] or "") in providers.ATTENTION]

    previous = previous_run(svc, run_date)
    since = previous["generated_at"] if previous else None
    closed_recently = _closed_since(svc, since or (run_date + "T00:00:00Z"))

    counts = svc.stats()

    # A container is an issue other issues hang off. It reads as in_progress
    # only because its children are, so counting it as work in progress
    # overstates the WIP and makes the limit meaningless. Nesting is a single
    # level (core.issues._resolve_parent), so any issue named as a parent is a
    # container -- including one whose children are all closed.
    container_keys = {
        i.parent_key
        for i in issues(has_parent=True, include_closed=True)
        if i.parent_key
    }
    wip = sum(1 for i in in_progress if i["key"] not in container_keys)
    payload = {
        "date": run_date,
        "generated_at": utcnow(),
        "timezone": tz,
        "sections": {
            "overdue": overdue,
            "due_today": due_today,
            "due_this_week": due_week,
            "in_progress": in_progress,
            "blocked": blocked,
            "stale": stale,
            "triage": triage,
            "closed_recently": closed_recently,
        },
        "counts": counts,
        "wip": {"count": wip, "limit": svc.config.wip_limit,
                "over": wip > svc.config.wip_limit},
        "stale_days": svc.config.stale_days,
        "targets_unmet": targets,
        "watches": watches,
        "watches_needing_attention": attention,
        "deltas": _deltas(counts, previous),
        "previous_date": previous["date"] if previous else None,
    }
    payload["summary"] = _summary(payload)
    return payload


def _closed_since(svc, since):
    rows = svc.conn.execute(
        "SELECT i.key, i.title, i.status, i.priority, i.closed_at "
        "FROM issue i WHERE i.closed_at IS NOT NULL AND i.closed_at >= ? "
        "ORDER BY i.closed_at DESC LIMIT 50",
        (since,),
    ).fetchall()
    return [dict(r) for r in rows]


def _deltas(counts, previous):
    if not previous:
        return None
    old = previous.get("counts", {})
    out = {"open": counts["open"] - old.get("open", 0)}
    for status, n in counts["by_status"].items():
        delta = n - old.get("by_status", {}).get(status, 0)
        if delta:
            out.setdefault("by_status", {})[status] = delta
    return out


def _summary(payload):
    s = payload["sections"]
    bits = []
    for key, label in (("overdue", "overdue"), ("due_today", "due today"),
                       ("blocked", "blocked"), ("stale", "stale")):
        if s[key]:
            bits.append(f"{len(s[key])} {label}")
    if payload.get("watches_needing_attention"):
        bits.append(f"{len(payload['watches_needing_attention'])} job(s) stopped")
    return ", ".join(bits) or "nothing needs attention"


# ------------------------------------------------------------------ persistence


def previous_run(svc, before_date):
    row = svc.conn.execute(
        "SELECT run_date, generated_at, payload_json FROM digest_run "
        "WHERE run_date < ? ORDER BY run_date DESC LIMIT 1",
        (before_date,),
    ).fetchone()
    if not row:
        return None
    payload = json.loads(row["payload_json"])
    return {"date": row["run_date"], "generated_at": row["generated_at"], **payload}


def get_run(svc, run_date):
    row = svc.conn.execute(
        "SELECT * FROM digest_run WHERE run_date=?", (run_date,)
    ).fetchone()
    if not row:
        return None
    return {
        "date": row["run_date"],
        "generated_at": row["generated_at"],
        "reviewed_at": row["reviewed_at"],
        "review_notes": row["review_notes"],
        "payload": json.loads(row["payload_json"]),
    }


def save(svc, payload, write_files=True):
    """Persist the digest run and optionally write the markdown/json files."""
    body = json.dumps(payload, indent=2, sort_keys=True)
    with tx(svc.conn):
        svc.conn.execute(
            "INSERT INTO digest_run(run_date, generated_at, payload_json)"
            " VALUES (?,?,?) ON CONFLICT(run_date) DO UPDATE SET"
            " generated_at=excluded.generated_at, payload_json=excluded.payload_json",
            (payload["date"], payload["generated_at"], body),
        )
    written = []
    if write_files:
        out = svc.config.digest_path
        out.mkdir(parents=True, exist_ok=True)
        md = out / f"{payload['date']}.md"
        js = out / f"{payload['date']}.json"
        md.write_text(render_markdown(payload))
        js.write_text(body + "\n")
        written = [str(md), str(js)]
    return written


def mark_reviewed(svc, run_date, notes=None):
    with tx(svc.conn):
        cur = svc.conn.execute(
            "UPDATE digest_run SET reviewed_at=?, review_notes=? WHERE run_date=?",
            (utcnow(), notes, run_date),
        )
    return cur.rowcount > 0


# -------------------------------------------------------------------- rendering


def _line(item, show=None):
    key = item["key"]
    bits = [f"  {key:<10} {item['priority']}  {item['title']}"]
    extra = []
    if show == "days_late":
        extra.append(f"{item['days_late']}d late")
    elif item.get("due_date"):
        extra.append(f"due {item['due_date']}")
    if item.get("assignee"):
        extra.append(f"@{item['assignee']}")
    if item.get("blocked_by"):
        extra.append("blocked by " + ", ".join(b["key"] for b in item["blocked_by"]))
    elif item.get("reason"):
        extra.append(f"reason: {item['reason']}")
    if item.get("labels"):
        extra.append(" ".join(f"#{l}" for l in item["labels"]))
    if extra:
        bits.append("             " + " · ".join(extra))
    return "\n".join(bits)


def render_markdown(payload):
    s = payload["sections"]
    out = [
        f"# TAM Daily — {payload['date']}",
        "",
        f"_{payload['summary']}_  ",
        f"generated {payload['generated_at']} ({payload['timezone']})",
        "",
    ]

    for key, title in SECTION_TITLES:
        items = s[key]
        if not items:
            continue
        note = ""
        if key == "stale":
            note = f" (no activity in {payload['stale_days']}d)"
        out.append(f"## {title}{note} ({len(items)})")
        out.append("")
        for item in items:
            if key == "closed_recently":
                out.append(f"  {item['key']:<10} {item['status']:<10} {item['title']}")
            else:
                out.append(_line(item, show="days_late" if key == "overdue" else None))
        out.append("")

    attention = payload.get("watches_needing_attention") or []
    if attention:
        out.append(f"## Jobs needing attention ({len(attention)})")
        out.append("")
        for w in attention:
            out.append(f"  {w['issue']:<10} {w['provider']}:{w['ref']}  ->  {w['state']}")
        out.append("")

    unmet = payload.get("targets_unmet") or []
    if unmet:
        out.append(f"## Targets not met ({len(unmet)})")
        out.append("")
        for t in unmet:
            actual = "no data" if t["actual"] is None else f"{t['actual']:.4f}"
            gap = "" if t["gap"] in (None, 0.0) else f"  (gap {t['gap']:+.4f})"
            out.append(f"  {t['issue']:<10} {t['metric']:<12} "
                       f"{t['op']} {t['value']}   actual {actual}{gap}")
        out.append("")

    wip = payload["wip"]
    if wip["over"]:
        out.append(f"> WIP is {wip['count']}, over the limit of {wip['limit']}.")
        out.append("")

    counts = payload["counts"]
    open_by_status = ", ".join(
        f"{k} {v}" for k, v in counts["by_status"].items()
        if v and k not in ("done", "cancelled")
    )
    out.append("## Counts")
    out.append("")
    out.append(f"  open {counts['open']} — {open_by_status or 'none'}")
    out.append(
        "  priority — "
        + ", ".join(f"{k} {v}" for k, v in counts["open_by_priority"].items() if v)
    )
    deltas = payload.get("deltas")
    if deltas:
        sign = "+" if deltas["open"] >= 0 else ""
        out.append(f"  open change since {payload['previous_date']}: {sign}{deltas['open']}")
    out.append("")
    return "\n".join(out)
