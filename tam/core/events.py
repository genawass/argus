"""Audit log writer and reader.

Every mutating call in core funnels through `record`. `issue_key` is
denormalised alongside `issue_id` on purpose: when an issue is deleted its
history must remain readable, and a dangling id is not readable.
"""

from ..clock import utcnow
from ..models import Event


def record(conn, *, issue_id, issue_key, actor, kind,
           field=None, old=None, new=None, note=None, at=None):
    conn.execute(
        "INSERT INTO event(issue_id, issue_key, actor, kind, field, old_value,"
        " new_value, note, at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            issue_id,
            issue_key,
            actor,
            kind,
            field,
            None if old is None else str(old),
            None if new is None else str(new),
            note,
            at or utcnow(),
        ),
    )


def history(conn, issue_key, limit=200):
    rows = conn.execute(
        "SELECT * FROM event WHERE issue_key=? ORDER BY at ASC, id ASC LIMIT ?",
        (issue_key, limit),
    ).fetchall()
    return [Event.from_row(r) for r in rows]


def recent(conn, since, limit=500):
    rows = conn.execute(
        "SELECT * FROM event WHERE at >= ? ORDER BY at DESC, id DESC LIMIT ?",
        (since, limit),
    ).fetchall()
    return [Event.from_row(r) for r in rows]
