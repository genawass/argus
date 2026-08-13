"""The single filter/sort engine.

CLI, HTTP, MCP and the digest all build an `IssueFilter` and hand it here. No
adapter writes SQL, and no adapter has a filter the others lack.
"""

from dataclasses import dataclass, field

from ..clock import days_ago, today
from ..db import has_fts
from ..errors import ValidationError
from ..models import CLOSED_STATUSES, OPEN_STATUSES, validate_priority, validate_status, validate_type
from .issues import BASE_SELECT, hydrate

SORT_COLUMNS = {
    "priority": "i.priority",
    "due_date": "i.due_date",
    "updated_at": "i.updated_at",
    "created_at": "i.created_at",
    "status": "i.status",
    "title": "i.title",
    "key": "p.key, i.seq",
}
DEFAULT_SORT = "priority"


@dataclass
class IssueFilter:
    project: str | None = None
    status: tuple = ()
    priority: tuple = ()
    type: tuple = ()
    assignee: str | None = None
    labels: tuple = ()
    text: str | None = None
    due_before: str | None = None
    due_after: str | None = None
    due_on: str | None = None
    overdue: bool = False
    stale_days: int | None = None
    updated_before: str | None = None
    created_after: str | None = None
    parent: str | None = None
    has_parent: bool | None = None
    is_blocked: bool = False
    include_closed: bool = False
    sort: str = DEFAULT_SORT
    order: str = "asc"
    limit: int | None = None
    offset: int = 0

    def validate(self):
        for s in self.status:
            validate_status(s)
        for p in self.priority:
            validate_priority(p)
        for t in self.type:
            validate_type(t)
        if self.sort not in SORT_COLUMNS:
            raise ValidationError(
                f"sort must be one of {', '.join(sorted(SORT_COLUMNS))}", field="sort"
            )
        if self.order not in ("asc", "desc"):
            raise ValidationError("order must be asc or desc", field="order")
        return self


MULTI_FIELDS = ("status", "priority", "type", "labels")
BOOL_FIELDS = ("overdue", "is_blocked", "include_closed")
INT_FIELDS = ("limit", "offset", "stale_days")
DATE_FIELDS = ("due_before", "due_after", "due_on")
STR_FIELDS = ("project", "assignee", "text", "parent", "sort", "order", "updated_before",
              "created_after")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def filter_from_params(params, tz):
    """Build an IssueFilter from loosely-typed input.

    Shared by the HTTP and MCP adapters so a filter can never exist in one
    interface and not the others. Accepts urllib's dict-of-lists as well as
    plain JSON values; multi-valued fields also accept a comma-separated string.
    """
    from ..clock import expand_date          # local import: avoids a cycle

    def raw(name):
        if name not in params:
            return None
        v = params[name]
        return v[-1] if isinstance(v, list) and len(v) == 1 else v

    def many(name):
        v = params.get(name)
        if v is None:
            return ()
        if isinstance(v, str):
            v = v.split(",")
        if not isinstance(v, (list, tuple)):
            v = [v]
        out = []
        for item in v:
            out.extend(str(item).split(",") if isinstance(item, str) else [item])
        return tuple(x.strip() for x in out if str(x).strip())

    def flag(name):
        v = raw(name)
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ValidationError(f"{name} must be a boolean", field=name)

    def number(name):
        v = raw(name)
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValidationError(f"{name} must be an integer", field=name)

    kwargs = {}
    for name in MULTI_FIELDS:
        value = many(name)
        if value:
            kwargs[name] = value
    for name in STR_FIELDS:
        value = raw(name)
        if value not in (None, ""):
            kwargs[name] = str(value)
    for name in DATE_FIELDS:
        value = raw(name)
        if value not in (None, ""):
            kwargs[name] = expand_date(str(value), tz)
    for name in BOOL_FIELDS:
        value = flag(name)
        if value is not None:
            kwargs[name] = value
    for name in INT_FIELDS:
        value = number(name)
        if value is not None:
            kwargs[name] = value

    has_parent = flag("has_parent")
    if has_parent is not None:
        kwargs["has_parent"] = has_parent

    unknown = set(params) - set(MULTI_FIELDS) - set(STR_FIELDS) - set(DATE_FIELDS) \
        - set(BOOL_FIELDS) - set(INT_FIELDS) - {"has_parent"}
    if unknown:
        raise ValidationError(f"unknown filter(s): {', '.join(sorted(unknown))}")

    return IssueFilter(**kwargs)


def _text_clause(conn, text):
    if has_fts(conn):
        # Quote the whole term so punctuation can never be read as FTS syntax.
        phrase = '"' + text.replace('"', '""') + '"'
        return "i.id IN (SELECT rowid FROM issue_fts WHERE issue_fts MATCH ?)", [phrase]
    like = f"%{text}%"
    return "(i.title LIKE ? OR i.body LIKE ?)", [like, like]


def build_where(ctx, f):
    f.validate()
    clauses, params = [], []

    if f.project:
        clauses.append("p.key = ?")
        params.append(f.project.strip().upper())

    if f.status:
        clauses.append(f"i.status IN ({','.join('?' * len(f.status))})")
        params.extend(f.status)
    elif not f.include_closed:
        clauses.append(f"i.status IN ({','.join('?' * len(OPEN_STATUSES))})")
        params.extend(OPEN_STATUSES)

    if f.priority:
        clauses.append(f"i.priority IN ({','.join('?' * len(f.priority))})")
        params.extend(f.priority)

    if f.type:
        clauses.append(f"i.type IN ({','.join('?' * len(f.type))})")
        params.extend(f.type)

    if f.assignee:
        if f.assignee in ("none", "unassigned"):
            clauses.append("(i.assignee IS NULL OR i.assignee = '')")
        else:
            clauses.append("i.assignee = ?")
            params.append(f.assignee)

    if f.labels:
        names = list(f.labels)
        clauses.append(
            "i.id IN (SELECT il.issue_id FROM issue_label il "
            "JOIN label l ON l.id = il.label_id "
            f"WHERE l.name IN ({','.join('?' * len(names))}) "
            "GROUP BY il.issue_id HAVING COUNT(DISTINCT l.name) = ?)"
        )
        params.extend(names)
        params.append(len(set(names)))

    if f.text:
        clause, p = _text_clause(ctx.conn, f.text)
        clauses.append(clause)
        params.extend(p)

    if f.due_before:
        clauses.append("i.due_date IS NOT NULL AND i.due_date < ?")
        params.append(f.due_before)
    if f.due_after:
        clauses.append("i.due_date IS NOT NULL AND i.due_date > ?")
        params.append(f.due_after)
    if f.due_on:
        clauses.append("i.due_date = ?")
        params.append(f.due_on)

    if f.overdue:
        clauses.append("i.due_date IS NOT NULL AND i.due_date < ?")
        params.append(today(ctx.config.timezone).isoformat())
        clauses.append(f"i.status NOT IN ({','.join('?' * len(CLOSED_STATUSES))})")
        params.extend(CLOSED_STATUSES)

    cutoff = f.updated_before
    if f.stale_days is not None:
        cutoff = days_ago(ctx.config.timezone, f.stale_days)
    if cutoff:
        clauses.append("i.updated_at < ?")
        params.append(cutoff)

    if f.created_after:
        clauses.append("i.created_at > ?")
        params.append(f.created_after)

    if f.parent:
        clauses.append("par.key = ?")
        params.append(f.parent.strip().upper())
    if f.has_parent is True:
        clauses.append("i.parent_id IS NOT NULL")
    elif f.has_parent is False:
        clauses.append("i.parent_id IS NULL")

    if f.is_blocked:
        clauses.append(
            "(i.status = 'blocked' OR EXISTS (SELECT 1 FROM issue_link il "
            "JOIN issue b ON b.id = il.to_issue WHERE il.from_issue = i.id "
            "AND il.type = 'blocked_by' AND b.status NOT IN ('done','cancelled')))"
        )

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _order_by(f):
    direction = "DESC" if f.order == "desc" else "ASC"
    if f.sort == "due_date":
        # Undated issues always sort last -- "no due date" is not "due first".
        return f"ORDER BY i.due_date IS NULL ASC, i.due_date {direction}, i.id ASC"
    return f"ORDER BY {SORT_COLUMNS[f.sort]} {direction}, i.id ASC"


def list_issues(ctx, f):
    where, params = build_where(ctx, f)
    sql = BASE_SELECT + where + " " + _order_by(f)
    if f.limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(f.limit), int(f.offset)])
    rows = ctx.conn.execute(sql, params).fetchall()
    return hydrate(ctx.conn, rows)


def count_issues(ctx, f):
    where, params = build_where(ctx, f)
    sql = (
        "SELECT COUNT(*) AS n FROM issue i JOIN project p ON p.id = i.project_id "
        "LEFT JOIN issue par ON par.id = i.parent_id" + where
    )
    return ctx.conn.execute(sql, params).fetchone()["n"]


def stats(ctx, project=None):
    where, params = "", []
    if project:
        where = " WHERE p.key = ?"
        params = [project.strip().upper()]
    base = " FROM issue i JOIN project p ON p.id = i.project_id" + where

    by_status = {
        r["status"]: r["n"]
        for r in ctx.conn.execute(
            "SELECT i.status, COUNT(*) AS n" + base + " GROUP BY i.status", params
        ).fetchall()
    }
    by_priority = {
        r["priority"]: r["n"]
        for r in ctx.conn.execute(
            "SELECT i.priority, COUNT(*) AS n" + base
            + (" AND" if where else " WHERE")
            + " i.status NOT IN ('done','cancelled') GROUP BY i.priority",
            params,
        ).fetchall()
    }
    open_count = sum(by_status.get(s, 0) for s in OPEN_STATUSES)
    return {
        "project": project,
        "total": sum(by_status.values()),
        "open": open_count,
        "closed": sum(by_status.get(s, 0) for s in CLOSED_STATUSES),
        "by_status": {s: by_status.get(s, 0) for s in ("backlog", "todo", "in_progress",
                                                       "blocked", "review", "done",
                                                       "cancelled")},
        "open_by_priority": {p: by_priority.get(p, 0) for p in ("p0", "p1", "p2", "p3")},
    }
