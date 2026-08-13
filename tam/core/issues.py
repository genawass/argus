"""Issue lifecycle: create, read, update, transition, delete, labels.

All workflow guards live here or in workflow.py. Adapters must not reimplement
any of it -- that rule is what keeps the CLI, HTTP and MCP surfaces identical.
"""

from ..clock import parse_date, utcnow
from ..db import tx
from ..errors import ConflictError, NotFoundError, TransitionError, ValidationError
from ..models import (
    CLOSED_STATUSES,
    Issue,
    parse_issue_key,
    validate_label,
    validate_priority,
    validate_status,
    validate_title,
    validate_type,
)
from . import events, projects
from .workflow import check_transition, closes

UNSET = object()

BASE_SELECT = """
SELECT i.*, p.key AS project_key, par.key AS parent_key
FROM issue i
JOIN project p ON p.id = i.project_id
LEFT JOIN issue par ON par.id = i.parent_id
"""


# --------------------------------------------------------------------------- read


def _row(ctx, key):
    key = (key or "").strip().upper()
    parse_issue_key(key)
    row = ctx.conn.execute(BASE_SELECT + " WHERE i.key = ?", (key,)).fetchone()
    if not row:
        raise NotFoundError(f"no issue {key}", key=key)
    return row


def labels_for(conn, issue_ids):
    """Fetch labels for a batch of issues in one query."""
    if not issue_ids:
        return {}
    marks = ",".join("?" * len(issue_ids))
    rows = conn.execute(
        f"SELECT il.issue_id, l.name FROM issue_label il "
        f"JOIN label l ON l.id = il.label_id "
        f"WHERE il.issue_id IN ({marks}) ORDER BY l.name",
        tuple(issue_ids),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["issue_id"], []).append(r["name"])
    return out


def hydrate(conn, rows):
    labels = labels_for(conn, [r["id"] for r in rows])
    return [Issue.from_row(r, labels.get(r["id"], [])) for r in rows]


def get_issue(ctx, key):
    row = _row(ctx, key)
    return hydrate(ctx.conn, [row])[0]


def children(ctx, key):
    parent = _row(ctx, key)
    rows = ctx.conn.execute(
        BASE_SELECT + " WHERE i.parent_id = ? ORDER BY i.seq", (parent["id"],)
    ).fetchall()
    return hydrate(ctx.conn, rows)


# ------------------------------------------------------------------------- write


def _resolve_parent(ctx, parent_key):
    """Resolve a parent key, enforcing a single level of nesting."""
    if parent_key is None:
        return None
    prow = _row(ctx, parent_key)
    if prow["parent_id"] is not None:
        raise ValidationError(
            f"{prow['key']} is itself a subtask; subtask nesting is one level deep",
            field="parent",
        )
    return prow


def _apply_labels(ctx, issue_id, names):
    ids = []
    for raw in names:
        name = validate_label(raw)
        ctx.conn.execute("INSERT OR IGNORE INTO label(name) VALUES (?)", (name,))
        row = ctx.conn.execute("SELECT id FROM label WHERE name=?", (name,)).fetchone()
        ids.append(row["id"])
    ctx.conn.execute("DELETE FROM issue_label WHERE issue_id=?", (issue_id,))
    for lid in ids:
        ctx.conn.execute(
            "INSERT OR IGNORE INTO issue_label(issue_id, label_id) VALUES (?,?)",
            (issue_id, lid),
        )


def create_issue(ctx, title, body="", type="task", status="backlog", priority="p2",
                 assignee=None, reporter=None, due_date=None, parent=None,
                 labels=(), project=None, external_ref=None, reason=None, actor=None):
    title = validate_title(title)
    type = validate_type(type)
    status = validate_status(status)
    priority = validate_priority(priority)
    due_date = parse_date(due_date, "due_date")
    actor = ctx.actor(actor)
    now = utcnow()

    # The same guard transition() applies: nothing enters `blocked` without a
    # stated cause. A new issue has no links yet, so a reason is the only option.
    if status == "blocked" and not reason:
        raise ValidationError(
            "creating an issue as blocked requires a reason", field="reason"
        )

    with tx(ctx.conn):
        proj = projects.resolve(ctx, project)
        if proj.archived_at:
            raise ConflictError(f"project {proj.key} is archived", key=proj.key)
        prow = _resolve_parent(ctx, parent)

        seq = ctx.conn.execute(
            "UPDATE project SET issue_seq = issue_seq + 1 WHERE id=? RETURNING issue_seq",
            (proj.id,),
        ).fetchone()["issue_seq"]
        key = f"{proj.key}-{seq}"

        cur = ctx.conn.execute(
            "INSERT INTO issue(project_id, seq, key, type, title, body, status,"
            " priority, assignee, reporter, due_date, parent_id, external_ref,"
            " created_at, updated_at, closed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                proj.id, seq, key, type, title, body or "", status, priority,
                assignee, reporter or actor, due_date,
                prow["id"] if prow else None, external_ref,
                now, now, now if status in CLOSED_STATUSES else None,
            ),
        )
        issue_id = cur.lastrowid
        if labels:
            _apply_labels(ctx, issue_id, labels)
        events.record(
            conn=ctx.conn, issue_id=issue_id, issue_key=key, actor=actor,
            kind="created", new=status, note=reason or title, at=now,
        )
    return get_issue(ctx, key)


FIELD_VALIDATORS = {
    "title": validate_title,
    "type": validate_type,
    "priority": validate_priority,
    "body": lambda v: v or "",
    "assignee": lambda v: v,
    "reporter": lambda v: v,
    "external_ref": lambda v: v,
    "due_date": lambda v: parse_date(v, "due_date"),
}


def update_issue(ctx, key, actor=None, labels=UNSET, parent=UNSET, **fields):
    """Patch scalar fields. Pass a value of None to clear a nullable field.

    `status` is deliberately rejected here: status changes must go through
    `transition` so the workflow matrix and its guards cannot be bypassed.
    """
    if "status" in fields:
        raise ValidationError(
            "status changes must use transition(), not update()", field="status"
        )
    actor = ctx.actor(actor)
    now = utcnow()

    with tx(ctx.conn):
        row = _row(ctx, key)
        changes = {}
        for name, value in fields.items():
            if value is UNSET:
                continue
            if name not in FIELD_VALIDATORS:
                raise ValidationError(f"unknown field {name!r}", field=name)
            new = FIELD_VALIDATORS[name](value) if value is not None else None
            if new != row[name]:
                changes[name] = new

        if parent is not UNSET:
            prow = _resolve_parent(ctx, parent) if parent else None
            new_parent = prow["id"] if prow else None
            if new_parent == row["id"]:
                raise ValidationError("an issue cannot be its own parent", field="parent")
            if new_parent != row["parent_id"]:
                if ctx.conn.execute(
                    "SELECT 1 FROM issue WHERE parent_id=? LIMIT 1", (row["id"],)
                ).fetchone() and new_parent is not None:
                    raise ValidationError(
                        f"{row['key']} has subtasks; it cannot become a subtask itself",
                        field="parent",
                    )
                changes["parent_id"] = new_parent
                events.record(
                    conn=ctx.conn, issue_id=row["id"], issue_key=row["key"], actor=actor,
                    kind="updated", field="parent", old=row["parent_key"],
                    new=prow["key"] if prow else None, at=now,
                )

        for name, new in changes.items():
            if name == "parent_id":
                continue
            events.record(
                conn=ctx.conn, issue_id=row["id"], issue_key=row["key"], actor=actor,
                kind="updated", field=name, old=row[name], new=new, at=now,
            )

        if labels is not UNSET:
            before = labels_for(ctx.conn, [row["id"]]).get(row["id"], [])
            after = sorted({validate_label(x) for x in labels})
            if before != after:
                _apply_labels(ctx, row["id"], after)
                events.record(
                    conn=ctx.conn, issue_id=row["id"], issue_key=row["key"], actor=actor,
                    kind="updated", field="labels",
                    old=",".join(before), new=",".join(after), at=now,
                )
                changes.setdefault("_labels", True)

        if changes:
            sets = [f"{n}=?" for n in changes if n != "_labels"]
            params = [v for n, v in changes.items() if n != "_labels"]
            sets.append("updated_at=?")
            params.append(now)
            params.append(row["id"])
            ctx.conn.execute(f"UPDATE issue SET {', '.join(sets)} WHERE id=?", params)

    return get_issue(ctx, key)


def transition(ctx, key, status, reason=None, force=False, actor=None):
    """Move an issue to a new status, enforcing the matrix and its guards."""
    status = validate_status(status)
    actor = ctx.actor(actor)
    now = utcnow()

    with tx(ctx.conn):
        row = _row(ctx, key)
        old = row["status"]
        if old == status:
            return get_issue(ctx, key)      # idempotent: agents retry

        check_transition(old, status)

        if status == "done" and not force:
            open_kids = ctx.conn.execute(
                "SELECT key FROM issue WHERE parent_id=? AND status NOT IN "
                "('done','cancelled') ORDER BY seq",
                (row["id"],),
            ).fetchall()
            if open_kids:
                raise TransitionError(
                    f"{row['key']} has {len(open_kids)} open subtask(s): "
                    f"{', '.join(k['key'] for k in open_kids)}; close them or use force",
                    open_subtasks=[k["key"] for k in open_kids],
                )

        if status == "blocked" and not reason:
            has_blocker = ctx.conn.execute(
                "SELECT 1 FROM issue_link WHERE from_issue=? AND type='blocked_by' LIMIT 1",
                (row["id"],),
            ).fetchone()
            if not has_blocker:
                raise ValidationError(
                    f"blocking {row['key']} requires a reason or a blocked_by link",
                    field="reason",
                )

        closed_at = now if closes(status) else None
        ctx.conn.execute(
            "UPDATE issue SET status=?, closed_at=?, updated_at=? WHERE id=?",
            (status, closed_at, now, row["id"]),
        )
        note = reason
        if force and status == "done":
            note = ((reason + " ") if reason else "") + "[forced over open subtasks]"
        events.record(
            conn=ctx.conn, issue_id=row["id"], issue_key=row["key"], actor=actor,
            kind="transitioned", field="status", old=old, new=status,
            note=note, at=now,
        )
    return get_issue(ctx, key)


def add_labels(ctx, key, names, actor=None):
    issue = get_issue(ctx, key)
    merged = sorted(set(issue.labels) | {validate_label(n) for n in names})
    return update_issue(ctx, key, labels=merged, actor=actor)


def remove_labels(ctx, key, names, actor=None):
    issue = get_issue(ctx, key)
    drop = {validate_label(n) for n in names}
    return update_issue(ctx, key, labels=sorted(set(issue.labels) - drop), actor=actor)


def list_labels(ctx):
    rows = ctx.conn.execute(
        "SELECT l.name, COUNT(il.issue_id) AS n FROM label l "
        "LEFT JOIN issue_label il ON il.label_id = l.id "
        "GROUP BY l.id ORDER BY l.name"
    ).fetchall()
    return [{"name": r["name"], "count": r["n"]} for r in rows]


def delete_issue(ctx, key, actor=None):
    """Hard-delete an issue. Its audit rows survive, keyed by issue_key."""
    actor = ctx.actor(actor)
    with tx(ctx.conn):
        row = _row(ctx, key)
        kids = ctx.conn.execute(
            "SELECT key FROM issue WHERE parent_id=?", (row["id"],)
        ).fetchall()
        ctx.conn.execute("DELETE FROM issue WHERE id=?", (row["id"],))
        events.record(
            conn=ctx.conn, issue_id=None, issue_key=row["key"], actor=actor,
            kind="deleted", old=row["status"], note=row["title"],
        )
    return {
        "key": row["key"],
        "deleted": True,
        "orphaned_subtasks": [k["key"] for k in kids],
    }
