"""Typed issue links.

Every link is stored as a pair: the stated direction and its inverse. Storing
both means "what blocks TAM-42" and "what does TAM-42 block" are the same
single-table lookup, at the cost of keeping the pair consistent on write --
which is why insertion and deletion both happen here and nowhere else.
"""

from ..clock import utcnow
from ..db import tx
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import LINK_INVERSE, Link, validate_link_type
from . import events
from .issues import _row


def _blocks_edge(link_type, from_id, to_id):
    """Normalise a link into a directed edge of the `blocks` graph, if it is one."""
    if link_type == "blocks":
        return (from_id, to_id)
    if link_type == "blocked_by":
        return (to_id, from_id)
    return None


def _reaches(conn, start, target):
    """Is `target` reachable from `start` following `blocks` edges?"""
    seen, stack = {start}, [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        rows = conn.execute(
            "SELECT to_issue FROM issue_link WHERE from_issue=? AND type='blocks'",
            (node,),
        ).fetchall()
        for r in rows:
            nxt = r["to_issue"]
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def add_link(ctx, from_key, to_key, link_type, actor=None):
    link_type = validate_link_type(link_type)
    actor = ctx.actor(actor)
    now = utcnow()

    with tx(ctx.conn):
        a, b = _row(ctx, from_key), _row(ctx, to_key)
        if a["id"] == b["id"]:
            raise ValidationError("an issue cannot be linked to itself", field="to")

        edge = _blocks_edge(link_type, a["id"], b["id"])
        if edge and _reaches(ctx.conn, edge[1], edge[0]):
            raise ConflictError(
                f"{a['key']} -> {b['key']} ({link_type}) would create a blocking cycle",
                from_key=a["key"], to_key=b["key"], type=link_type,
            )

        inverse = LINK_INVERSE[link_type]
        existing = ctx.conn.execute(
            "SELECT 1 FROM issue_link WHERE from_issue=? AND to_issue=? AND type=?",
            (a["id"], b["id"], link_type),
        ).fetchone()
        if existing:
            return list_links(ctx, a["key"])

        ctx.conn.execute(
            "INSERT OR IGNORE INTO issue_link(from_issue,to_issue,type,created_at)"
            " VALUES (?,?,?,?)",
            (a["id"], b["id"], link_type, now),
        )
        ctx.conn.execute(
            "INSERT OR IGNORE INTO issue_link(from_issue,to_issue,type,created_at)"
            " VALUES (?,?,?,?)",
            (b["id"], a["id"], inverse, now),
        )
        for src, dst, typ in ((a, b, link_type), (b, a, inverse)):
            ctx.conn.execute(
                "UPDATE issue SET updated_at=? WHERE id=?", (now, src["id"])
            )
            events.record(
                conn=ctx.conn, issue_id=src["id"], issue_key=src["key"], actor=actor,
                kind="linked", field=typ, new=dst["key"], at=now,
            )
    return list_links(ctx, from_key)


def remove_link(ctx, from_key, to_key, link_type, actor=None):
    link_type = validate_link_type(link_type)
    actor = ctx.actor(actor)
    now = utcnow()

    with tx(ctx.conn):
        a, b = _row(ctx, from_key), _row(ctx, to_key)
        hit = ctx.conn.execute(
            "SELECT 1 FROM issue_link WHERE from_issue=? AND to_issue=? AND type=?",
            (a["id"], b["id"], link_type),
        ).fetchone()
        if not hit:
            raise NotFoundError(
                f"no {link_type} link from {a['key']} to {b['key']}",
                from_key=a["key"], to_key=b["key"], type=link_type,
            )
        inverse = LINK_INVERSE[link_type]
        ctx.conn.execute(
            "DELETE FROM issue_link WHERE from_issue=? AND to_issue=? AND type=?",
            (a["id"], b["id"], link_type),
        )
        ctx.conn.execute(
            "DELETE FROM issue_link WHERE from_issue=? AND to_issue=? AND type=?",
            (b["id"], a["id"], inverse),
        )
        for src, dst, typ in ((a, b, link_type), (b, a, inverse)):
            ctx.conn.execute(
                "UPDATE issue SET updated_at=? WHERE id=?", (now, src["id"])
            )
            events.record(
                conn=ctx.conn, issue_id=src["id"], issue_key=src["key"], actor=actor,
                kind="unlinked", field=typ, old=dst["key"], at=now,
            )
    return list_links(ctx, from_key)


def list_links(ctx, key):
    row = _row(ctx, key)
    rows = ctx.conn.execute(
        "SELECT il.type, il.created_at, i.key AS to_key, i.title, i.status "
        "FROM issue_link il JOIN issue i ON i.id = il.to_issue "
        "WHERE il.from_issue = ? ORDER BY il.type, i.key",
        (row["id"],),
    ).fetchall()
    return [
        Link(
            type=r["type"], from_key=row["key"], to_key=r["to_key"],
            to_title=r["title"], to_status=r["status"], created_at=r["created_at"],
        )
        for r in rows
    ]


def blockers(ctx, key, open_only=True):
    """Issues that are blocking `key`."""
    return [
        l for l in list_links(ctx, key)
        if l.type == "blocked_by"
        and (not open_only or l.to_status not in ("done", "cancelled"))
    ]
