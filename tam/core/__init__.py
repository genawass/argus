"""Service facade -- the single entry point every adapter uses.

A `Service` instance is the `ctx` that the core modules take: it carries the
connection and the config, and resolves the acting identity. Adapters build one
of these and call methods on it. They never touch `conn` directly.
"""

import os
import socket

from .. import config as config_mod
from .. import db as db_mod
from ..errors import ConflictError
from . import comments, events, issues, links, observe, projects, query
from .query import IssueFilter
from .workflow import TRANSITIONS, allowed_from

__all__ = ["Service", "IssueFilter", "TRANSITIONS", "allowed_from", "check_db_owner"]


def check_db_owner(config):
    """Refuse to open a shared database from a host that does not own it.

    SQLite on a network filesystem cannot be opened safely from two machines:
    WAL's shared-memory file is not shared across hosts, and rollback-journal
    mode still drops writes under contention. Rather than let a second host
    lose updates silently, fail loudly and point at the API.

    TAM_ALLOW_FOREIGN_DB=1 overrides this for deliberate maintenance.
    """
    owner = config.db_host
    if not owner or os.environ.get("TAM_ALLOW_FOREIGN_DB") == "1":
        return
    if not db_mod.is_network_fs(config.db_path):
        return
    here = socket.gethostname()
    if here == owner:
        return
    raise ConflictError(
        f"{config.db_path} is owned by {owner}; this is {here}. Opening a "
        f"shared SQLite database from a second host loses writes. Use the API "
        f"on {owner} instead (see README 'Cluster access'), or set "
        f"TAM_ALLOW_FOREIGN_DB=1 if {owner} is definitely not running.",
        owner=owner, host=here, db=str(config.db_path),
    )


class Service:
    def __init__(self, config=None, conn=None):
        self.config = config or config_mod.load()
        if conn is None:
            check_db_owner(self.config)
        self.conn = conn or db_mod.connect(self.config.db_path)

    # -- identity ---------------------------------------------------------
    def actor(self, override=None):
        return override or self.config.actor

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- projects ---------------------------------------------------------
    def create_project(self, key, name=None, description=""):
        return projects.create_project(self, key, name, description)

    def get_project(self, key):
        return projects.get_project(self, key)

    def list_projects(self, include_archived=False):
        return projects.list_projects(self, include_archived)

    def archive_project(self, key, unarchive=False):
        return projects.archive_project(self, key, unarchive)

    # -- issues -----------------------------------------------------------
    def create_issue(self, **kw):
        return issues.create_issue(self, **kw)

    def get_issue(self, key):
        return issues.get_issue(self, key)

    def update_issue(self, key, **kw):
        return issues.update_issue(self, key, **kw)

    def transition(self, key, status, reason=None, force=False, actor=None):
        return issues.transition(self, key, status, reason, force, actor)

    def delete_issue(self, key, actor=None):
        return issues.delete_issue(self, key, actor)

    def children(self, key):
        return issues.children(self, key)

    def add_labels(self, key, names, actor=None):
        return issues.add_labels(self, key, names, actor)

    def remove_labels(self, key, names, actor=None):
        return issues.remove_labels(self, key, names, actor)

    def list_labels(self):
        return issues.list_labels(self)

    # -- comments / links / history ---------------------------------------
    def add_comment(self, key, body, author=None):
        return comments.add_comment(self, key, body, author)

    def list_comments(self, key):
        return comments.list_comments(self, key)

    def add_link(self, from_key, to_key, link_type, actor=None):
        return links.add_link(self, from_key, to_key, link_type, actor)

    def remove_link(self, from_key, to_key, link_type, actor=None):
        return links.remove_link(self, from_key, to_key, link_type, actor)

    def list_links(self, key):
        return links.list_links(self, key)

    def blockers(self, key, open_only=True):
        return links.blockers(self, key, open_only)

    def history(self, key, limit=200):
        return events.history(self.conn, (key or "").strip().upper(), limit)

    def recent_events(self, since, limit=500):
        return events.recent(self.conn, since, limit)

    # -- query ------------------------------------------------------------
    def list_issues(self, f=None, **kw):
        return query.list_issues(self, f or IssueFilter(**kw))

    def count_issues(self, f=None, **kw):
        return query.count_issues(self, f or IssueFilter(**kw))

    def stats(self, project=None):
        return query.stats(self, project)

    # -- digest ------------------------------------------------------------
    # Exposed on the service (not called as module functions from adapters) so
    # that the remote implementation can satisfy the same surface over HTTP.
    def digest(self, date=None, write=False):
        from .. import digest as digest_mod
        payload = digest_mod.build(self, date)
        if write:
            digest_mod.save(self, payload)
        return payload

    def digest_run(self, date):
        from .. import digest as digest_mod
        return digest_mod.get_run(self, date)

    def review(self, date, notes=None):
        from .. import digest as digest_mod
        if not digest_mod.get_run(self, date):
            digest_mod.save(self, digest_mod.build(self, date))
        digest_mod.mark_reviewed(self, date, notes)
        return digest_mod.get_run(self, date)

    # -- watches / metrics / targets ---------------------------------------
    def add_watch(self, key, provider, ref, host=None, label=None, config=None,
                  actor=None):
        return observe.add_watch(self, key, provider, ref, host, label, config,
                                 actor)

    def remove_watch(self, watch_id, actor=None):
        return observe.remove_watch(self, watch_id, actor)

    def list_watches(self, key=None, provider=None):
        return observe.list_watches(self, key, provider)

    def providers(self):
        from .. import providers as reg
        reg.load_user_providers(self.config.data_dir / "providers")
        return reg.describe()

    def record_metric(self, key, name, value, step=None, source=None, at=None):
        return observe.record_metric(self, key, name, value, step, source, at)

    def list_metrics(self, key, name=None, limit=200):
        return observe.list_metrics(self, key, name, limit)

    def latest_metrics(self, key):
        return observe.latest_metrics(self, key)

    def metric_trend(self, key, name, window=10):
        return observe.metric_trend(self, key, name, window)

    def set_target(self, key, metric, op, value, note=None, actor=None):
        return observe.set_target(self, key, metric, op, value, note, actor)

    def remove_target(self, key, metric):
        return observe.remove_target(self, key, metric)

    def list_targets(self, key=None):
        return observe.list_targets(self, key)

    def evaluate_targets(self, key=None):
        return observe.evaluate_targets(self, key)

    def set_params(self, key, params, source=None, actor=None):
        return observe.set_params(self, key, params, source, actor)

    def list_params(self, key):
        return observe.list_params(self, key)

    def remove_param(self, key, name):
        return observe.remove_param(self, key, name)

    def heartbeat(self, key, ref="run", **kw):
        return observe.heartbeat(self, key, ref, **kw)

    def scan(self, key=None, record_metrics=True):
        from .. import scan as scan_mod
        return scan_mod.scan(self, key, record_metrics)

    def backup(self, dest=None, keep=14):
        from .. import backup as backup_mod
        return backup_mod.run(self.config, dest, keep)

    def workflow(self):
        from ..models import (CLOSED_STATUSES, ISSUE_TYPES, OPEN_STATUSES,
                              PRIORITIES, PRIORITY_LABEL, STATUSES)
        from ..clock import today
        return {
            "statuses": list(STATUSES),
            "open_statuses": list(OPEN_STATUSES),
            "closed_statuses": list(CLOSED_STATUSES),
            "priorities": list(PRIORITIES),
            "priority_labels": PRIORITY_LABEL,
            "types": list(ISSUE_TYPES),
            "transitions": {s: allowed_from(s) for s in STATUSES},
            "wip_limit": self.config.wip_limit,
            "stale_days": self.config.stale_days,
            "timezone": self.config.timezone,
            "today": today(self.config.timezone).isoformat(),
            "default_project": self.config.default_project,
        }
