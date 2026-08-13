"""Enums, validation, and the record types returned by core.

Core returns these dataclasses; adapters call `.to_dict()` and serialise. The
dataclasses exist so that a field rename is a type error in one place rather
than a silently missing JSON key in three.
"""

import re
from dataclasses import dataclass, field

from .errors import ValidationError

STATUSES = (
    "backlog",
    "todo",
    "in_progress",
    "blocked",
    "review",
    "done",
    "cancelled",
)
CLOSED_STATUSES = ("done", "cancelled")
OPEN_STATUSES = tuple(s for s in STATUSES if s not in CLOSED_STATUSES)

PRIORITIES = ("p0", "p1", "p2", "p3")
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}
PRIORITY_LABEL = {"p0": "critical", "p1": "high", "p2": "medium", "p3": "low"}

ISSUE_TYPES = ("task", "bug", "chore", "spike")

LINK_TYPES = ("blocks", "blocked_by", "relates_to", "duplicates", "duplicated_by")
LINK_INVERSE = {
    "blocks": "blocked_by",
    "blocked_by": "blocks",
    "duplicates": "duplicated_by",
    "duplicated_by": "duplicates",
    "relates_to": "relates_to",
}

PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]{1,9})-(\d+)$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,39}$")


def _one_of(value, allowed, field_name):
    if value not in allowed:
        raise ValidationError(
            f"{field_name} must be one of {', '.join(allowed)}; got {value!r}",
            field=field_name,
        )
    return value


def validate_status(v):
    return _one_of(v, STATUSES, "status")


def validate_priority(v):
    return _one_of(v, PRIORITIES, "priority")


def validate_type(v):
    return _one_of(v, ISSUE_TYPES, "type")


def validate_link_type(v):
    return _one_of(v, LINK_TYPES, "link type")


def validate_project_key(v):
    v = (v or "").strip().upper()
    if not PROJECT_KEY_RE.match(v):
        raise ValidationError(
            "project key must be 2-10 chars, A-Z then A-Z0-9 (e.g. TAM)",
            field="key",
        )
    return v


def parse_issue_key(v):
    """Split 'TAM-42' into ('TAM', 42)."""
    m = ISSUE_KEY_RE.match((v or "").strip().upper())
    if not m:
        raise ValidationError(f"malformed issue key {v!r}; expected e.g. TAM-42", field="key")
    return m.group(1), int(m.group(2))


def validate_label(v):
    v = (v or "").strip().lower()
    if not LABEL_RE.match(v):
        raise ValidationError(
            "label must be lowercase alphanumeric with . _ - / (max 40 chars)",
            field="label",
        )
    return v


def validate_title(v):
    v = (v or "").strip()
    if not v:
        raise ValidationError("title must not be empty", field="title")
    if len(v) > 500:
        raise ValidationError("title must be 500 characters or fewer", field="title")
    return v


@dataclass(frozen=True)
class Project:
    id: int
    key: str
    name: str
    description: str
    issue_seq: int
    created_at: str
    archived_at: str | None

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            key=row["key"],
            name=row["name"],
            description=row["description"],
            issue_seq=row["issue_seq"],
            created_at=row["created_at"],
            archived_at=row["archived_at"],
        )

    def to_dict(self):
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "issue_count": self.issue_seq,
            "created_at": self.created_at,
            "archived_at": self.archived_at,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=None, key=d["key"], name=d["name"],
            description=d.get("description", ""),
            issue_seq=d.get("issue_count", 0),
            created_at=d.get("created_at"), archived_at=d.get("archived_at"),
        )


@dataclass(frozen=True)
class Issue:
    id: int
    key: str
    project_key: str
    seq: int
    type: str
    title: str
    body: str
    status: str
    priority: str
    assignee: str | None
    reporter: str | None
    due_date: str | None
    parent_key: str | None
    external_ref: str | None
    created_at: str
    updated_at: str
    closed_at: str | None
    labels: tuple = ()

    @classmethod
    def from_row(cls, row, labels=()):
        keys = row.keys()
        return cls(
            id=row["id"],
            key=row["key"],
            project_key=row["project_key"] if "project_key" in keys else None,
            seq=row["seq"],
            type=row["type"],
            title=row["title"],
            body=row["body"],
            status=row["status"],
            priority=row["priority"],
            assignee=row["assignee"],
            reporter=row["reporter"],
            due_date=row["due_date"],
            parent_key=row["parent_key"] if "parent_key" in keys else None,
            external_ref=row["external_ref"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            closed_at=row["closed_at"],
            labels=tuple(labels),
        )

    @property
    def is_open(self):
        return self.status in OPEN_STATUSES

    def to_dict(self):
        return {
            "key": self.key,
            "project": self.project_key,
            "type": self.type,
            "title": self.title,
            "body": self.body,
            "status": self.status,
            "priority": self.priority,
            "assignee": self.assignee,
            "reporter": self.reporter,
            "due_date": self.due_date,
            "parent": self.parent_key,
            "external_ref": self.external_ref,
            "labels": list(self.labels),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, d):
        """Rebuild from the API's JSON form.

        `id` and `seq` are storage details the API does not publish; seq is
        recoverable from the key and id is unused outside the database.
        """
        try:
            seq = parse_issue_key(d["key"])[1]
        except ValidationError:
            seq = None
        return cls(
            id=None, key=d["key"], project_key=d.get("project"), seq=seq,
            type=d["type"], title=d["title"], body=d.get("body", ""),
            status=d["status"], priority=d["priority"],
            assignee=d.get("assignee"), reporter=d.get("reporter"),
            due_date=d.get("due_date"), parent_key=d.get("parent"),
            external_ref=d.get("external_ref"),
            created_at=d.get("created_at"), updated_at=d.get("updated_at"),
            closed_at=d.get("closed_at"), labels=tuple(d.get("labels") or ()),
        )


@dataclass(frozen=True)
class Comment:
    id: int
    issue_key: str
    author: str
    body: str
    created_at: str

    @classmethod
    def from_row(cls, row, issue_key=None):
        keys = row.keys()
        return cls(
            id=row["id"],
            issue_key=issue_key or (row["issue_key"] if "issue_key" in keys else None),
            author=row["author"],
            body=row["body"],
            created_at=row["created_at"],
        )

    def to_dict(self):
        return {
            "id": self.id,
            "issue": self.issue_key,
            "author": self.author,
            "body": self.body,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get("id"), issue_key=d.get("issue"), author=d["author"],
                   body=d["body"], created_at=d.get("created_at"))


@dataclass(frozen=True)
class Link:
    type: str
    from_key: str
    to_key: str
    to_title: str | None = None
    to_status: str | None = None
    created_at: str | None = None

    def to_dict(self):
        return {
            "type": self.type,
            "from": self.from_key,
            "to": self.to_key,
            "to_title": self.to_title,
            "to_status": self.to_status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(type=d["type"], from_key=d.get("from"), to_key=d["to"],
                   to_title=d.get("to_title"), to_status=d.get("to_status"),
                   created_at=d.get("created_at"))


@dataclass(frozen=True)
class Event:
    id: int
    issue_key: str | None
    actor: str
    kind: str
    field_name: str | None
    old_value: str | None
    new_value: str | None
    note: str | None
    at: str

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            issue_key=row["issue_key"],
            actor=row["actor"],
            kind=row["kind"],
            field_name=row["field"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            note=row["note"],
            at=row["at"],
        )

    def to_dict(self):
        return {
            "id": self.id,
            "issue": self.issue_key,
            "actor": self.actor,
            "kind": self.kind,
            "field": self.field_name,
            "old": self.old_value,
            "new": self.new_value,
            "note": self.note,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(id=d.get("id"), issue_key=d.get("issue"), actor=d["actor"],
                   kind=d["kind"], field_name=d.get("field"),
                   old_value=d.get("old"), new_value=d.get("new"),
                   note=d.get("note"), at=d.get("at"))
