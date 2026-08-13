"""Project CRUD and default-project resolution."""

from ..clock import utcnow
from ..db import tx
from ..errors import ConflictError, NotFoundError
from ..models import Project, validate_project_key


def create_project(ctx, key, name=None, description=""):
    key = validate_project_key(key)
    name = (name or key).strip()
    with tx(ctx.conn):
        exists = ctx.conn.execute(
            "SELECT 1 FROM project WHERE key=?", (key,)
        ).fetchone()
        if exists:
            raise ConflictError(f"project {key} already exists", key=key)
        ctx.conn.execute(
            "INSERT INTO project(key, name, description, created_at) VALUES (?,?,?,?)",
            (key, name, description or "", utcnow()),
        )
    return get_project(ctx, key)


def get_project(ctx, key):
    key = (key or "").strip().upper()
    row = ctx.conn.execute("SELECT * FROM project WHERE key=?", (key,)).fetchone()
    if not row:
        raise NotFoundError(f"no project {key}", key=key)
    return Project.from_row(row)


def list_projects(ctx, include_archived=False):
    sql = "SELECT * FROM project"
    if not include_archived:
        sql += " WHERE archived_at IS NULL"
    sql += " ORDER BY key"
    return [Project.from_row(r) for r in ctx.conn.execute(sql).fetchall()]


def archive_project(ctx, key, unarchive=False):
    project = get_project(ctx, key)
    with tx(ctx.conn):
        ctx.conn.execute(
            "UPDATE project SET archived_at=? WHERE id=?",
            (None if unarchive else utcnow(), project.id),
        )
    return get_project(ctx, key)


def resolve(ctx, key=None):
    """Resolve an explicit project key, or fall back to the configured default."""
    return get_project(ctx, key or ctx.config.default_project)
