"""Issue comments.

Commenting touches the issue's `updated_at`: a discussion is activity, and the
staleness detector in the digest should treat it as such.
"""

from ..clock import utcnow
from ..db import tx
from ..errors import ValidationError
from ..models import Comment
from . import events
from .issues import _row


def add_comment(ctx, key, body, author=None):
    body = (body or "").strip()
    if not body:
        raise ValidationError("comment body must not be empty", field="body")
    author = ctx.actor(author)
    now = utcnow()

    with tx(ctx.conn):
        row = _row(ctx, key)
        cur = ctx.conn.execute(
            "INSERT INTO comment(issue_id, author, body, created_at) VALUES (?,?,?,?)",
            (row["id"], author, body, now),
        )
        ctx.conn.execute("UPDATE issue SET updated_at=? WHERE id=?", (now, row["id"]))
        events.record(
            conn=ctx.conn, issue_id=row["id"], issue_key=row["key"], actor=author,
            kind="commented", note=body[:200], at=now,
        )
    return Comment(
        id=cur.lastrowid, issue_key=row["key"], author=author, body=body, created_at=now
    )


def list_comments(ctx, key):
    row = _row(ctx, key)
    rows = ctx.conn.execute(
        "SELECT * FROM comment WHERE issue_id=? ORDER BY created_at, id", (row["id"],)
    ).fetchall()
    return [Comment.from_row(r, issue_key=row["key"]) for r in rows]
