"""Remote service: the same surface as `core.Service`, spoken over HTTP.

Why this exists: the database is one file on one host's local disk, because only
one host may open a SQLite file safely (see README, "Cluster access"). Every
other node therefore has to go through the API. Rather than write a second
client with its own idea of what a command means, this presents the identical
method surface, so `cli.py` works against a remote database with no changes and
no branching.

It holds no rules. Validation, the workflow matrix and the guards all still
happen on the server; this only ferries arguments and rebuilds the records.
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .errors import TamError, ValidationError
from .models import Comment, Event, Issue, Link, Project

ERROR_CLASSES = {}


def _error_for(code, message, details):
    """Rebuild the server's error as the matching local exception type."""
    if not ERROR_CLASSES:
        from . import errors as errmod
        for name in dir(errmod):
            cls = getattr(errmod, name)
            if isinstance(cls, type) and issubclass(cls, TamError):
                ERROR_CLASSES[cls.code] = cls
    cls = ERROR_CLASSES.get(code, TamError)
    return cls(message, **(details or {}))


@dataclass
class RemoteConfig:
    """The subset of Config the adapters read, filled from /api/workflow."""

    api_url: str
    timezone: str = "UTC"
    wip_limit: int = 3
    stale_days: int = 7
    default_project: str = "TAM"
    actor: str = field(default_factory=lambda: __import__("socket")
                       .gethostname() and f"agent@{__import__('socket').gethostname()}")
    remote: bool = True

    def to_dict(self):
        return {
            "mode": "remote",
            "api_url": self.api_url,
            "default_project": self.default_project,
            "actor": self.actor,
            "timezone": self.timezone,
            "stale_days": self.stale_days,
            "wip_limit": self.wip_limit,
        }


def _flatten(value):
    """Render a filter value as query-string friendly text."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class RemoteService:
    def __init__(self, api_url, token=None, actor=None, timeout=20):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.conn = None                 # no local database
        self.config = RemoteConfig(api_url=self.api_url)
        if actor:
            self.config.actor = actor
        self._load_workflow()

    # -- transport --------------------------------------------------------
    def _call(self, method, path, body=None, params=None):
        url = self.api_url + path
        if params:
            from urllib.parse import urlencode
            pairs = [(k, _flatten(v)) for k, v in params.items() if v not in (None, (), [])]
            if pairs:
                url += "?" + urlencode(pairs)
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.token}"} if self.token else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except (ValueError, OSError):
                raise TamError(f"HTTP {exc.code} from {url}")
            err = payload.get("error") or {}
            raise _error_for(err.get("code"), err.get("message", f"HTTP {exc.code}"),
                             err.get("details"))
        except urllib.error.URLError as exc:
            raise TamError(
                f"cannot reach the TAM API at {self.api_url} ({exc.reason}). "
                f"Is tam-api running on the database owner host?"
            )
        if not payload.get("ok", True):
            err = payload.get("error") or {}
            raise _error_for(err.get("code"), err.get("message", "request failed"),
                             err.get("details"))
        return payload.get("data"), payload.get("meta") or {}

    def _load_workflow(self):
        wf, _ = self._call("GET", "/api/workflow")
        for name in ("timezone", "wip_limit", "stale_days", "default_project"):
            if name in wf:
                setattr(self.config, name, wf[name])

    def actor(self, override=None):
        return override or self.config.actor

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- projects ---------------------------------------------------------
    def create_project(self, key, name=None, description=""):
        data, _ = self._call("POST", "/api/projects",
                             {"key": key, "name": name, "description": description})
        return Project.from_dict(data)

    def get_project(self, key):
        data, _ = self._call("GET", f"/api/projects/{key}")
        return Project.from_dict(data)

    def list_projects(self, include_archived=False):
        data, _ = self._call("GET", "/api/projects",
                             params={"all": "1"} if include_archived else None)
        return [Project.from_dict(p) for p in data]

    def archive_project(self, key, unarchive=False):
        data, _ = self._call("POST", f"/api/projects/{key}/archive",
                             {"unarchive": bool(unarchive)})
        return Project.from_dict(data)

    # -- issues -----------------------------------------------------------
    def create_issue(self, **kw):
        kw = {k: v for k, v in kw.items() if v is not None and v != ()}
        if "labels" in kw:
            kw["labels"] = list(kw["labels"])
        kw.setdefault("actor", self.config.actor)
        data, _ = self._call("POST", "/api/issues", kw)
        return Issue.from_dict(data)

    def get_issue(self, key):
        data, _ = self._call("GET", f"/api/issues/{key}")
        return Issue.from_dict(data)

    def _issue_detail(self, key, comments=False, history=False):
        params = {}
        if comments:
            params["comments"] = "1"
        if history:
            params["history"] = "1"
        data, _ = self._call("GET", f"/api/issues/{key}", params=params or None)
        return data

    def update_issue(self, key, **kw):
        kw["actor"] = kw.get("actor") or self.config.actor
        if "labels" in kw and kw["labels"] is not None:
            kw["labels"] = list(kw["labels"])
        data, _ = self._call("PATCH", f"/api/issues/{key}", kw)
        return Issue.from_dict(data)

    def transition(self, key, status, reason=None, force=False, actor=None):
        body = {"status": status, "force": bool(force),
                "actor": actor or self.config.actor}
        if reason:
            body["reason"] = reason
        data, _ = self._call("POST", f"/api/issues/{key}/transition", body)
        return Issue.from_dict(data)

    def delete_issue(self, key, actor=None):
        data, _ = self._call("DELETE", f"/api/issues/{key}",
                             {"actor": actor or self.config.actor})
        return data

    def children(self, key):
        return [Issue.from_dict(s) for s in self._issue_detail(key).get("subtasks", [])]

    def add_labels(self, key, names, actor=None):
        issue = self.get_issue(key)
        return self.update_issue(key, labels=sorted(set(issue.labels) | set(names)))

    def remove_labels(self, key, names, actor=None):
        issue = self.get_issue(key)
        return self.update_issue(key, labels=sorted(set(issue.labels) - set(names)))

    def list_labels(self):
        data, _ = self._call("GET", "/api/labels")
        return data

    # -- comments / links / history ---------------------------------------
    def add_comment(self, key, body, author=None):
        payload = {"body": body, "author": author or self.config.actor}
        data, _ = self._call("POST", f"/api/issues/{key}/comments", payload)
        return Comment.from_dict(data)

    def list_comments(self, key):
        data, _ = self._call("GET", f"/api/issues/{key}/comments")
        return [Comment.from_dict(c) for c in data]

    def add_link(self, from_key, to_key, link_type, actor=None):
        data, _ = self._call("POST", f"/api/issues/{from_key}/links",
                             {"to": to_key, "type": link_type,
                              "actor": actor or self.config.actor})
        return [Link.from_dict(l) for l in data]

    def remove_link(self, from_key, to_key, link_type, actor=None):
        data, _ = self._call("DELETE", f"/api/issues/{from_key}/links",
                             {"to": to_key, "type": link_type,
                              "actor": actor or self.config.actor})
        return [Link.from_dict(l) for l in data]

    def list_links(self, key):
        data, _ = self._call("GET", f"/api/issues/{key}/links")
        return [Link.from_dict(l) for l in data]

    def blockers(self, key, open_only=True):
        return [
            l for l in self.list_links(key)
            if l.type == "blocked_by"
            and (not open_only or l.to_status not in ("done", "cancelled"))
        ]

    def history(self, key, limit=200):
        data, _ = self._call("GET", f"/api/issues/{key}/history")
        return [Event.from_dict(e) for e in data]

    # -- query ------------------------------------------------------------
    def _filter_params(self, f):
        from .core.query import IssueFilter
        default = IssueFilter()
        params = {}
        for name in vars(default):
            value = getattr(f, name)
            if value != getattr(default, name):
                params[name] = value
        return params

    def list_issues(self, f=None, **kw):
        from .core.query import IssueFilter
        f = f or IssueFilter(**kw)
        f.validate()
        data, _ = self._call("GET", "/api/issues", params=self._filter_params(f))
        return [Issue.from_dict(i) for i in data]

    def count_issues(self, f=None, **kw):
        from .core.query import IssueFilter
        f = f or IssueFilter(**kw)
        _, meta = self._call("GET", "/api/issues", params=self._filter_params(f))
        return meta.get("count", 0)

    def stats(self, project=None):
        data, _ = self._call("GET", "/api/stats",
                             params={"project": project} if project else None)
        return data

    def next_up(self, wip_limit=None):
        data, _ = self._call("GET", "/api/next",
                             params={"wip_limit": wip_limit} if wip_limit else None)
        return data

    # -- digest -----------------------------------------------------------
    def digest(self, date=None, write=False):
        params = {}
        if date:
            params["date"] = date
        if write:
            params["write"] = "1"
        data, _ = self._call("GET", "/api/digest", params=params or None)
        return data

    def digest_run(self, date):
        from .errors import NotFoundError
        try:
            data, _ = self._call("GET", f"/api/digest/{date}/run")
            return data
        except NotFoundError:
            return None

    def review(self, date, notes=None):
        data, _ = self._call("POST", f"/api/digest/{date}/review", {"notes": notes})
        return data

    # -- watches / metrics / targets ---------------------------------------
    def add_watch(self, key, provider, ref, host=None, label=None, config=None,
                  actor=None):
        data, _ = self._call("POST", f"/api/issues/{key}/watches",
                             {"provider": provider, "ref": ref, "host": host,
                              "label": label, "config": config,
                              "actor": actor or self.config.actor})
        return data

    def remove_watch(self, watch_id, actor=None):
        data, _ = self._call("DELETE", f"/api/watches/{watch_id}")
        return data

    def list_watches(self, key=None, provider=None):
        data, _ = self._call("GET", "/api/watches",
                             params={"issue": key, "provider": provider})
        return data

    def providers(self):
        data, _ = self._call("GET", "/api/providers")
        return data

    def record_metric(self, key, name, value, step=None, source=None, at=None):
        data, _ = self._call("POST", f"/api/issues/{key}/metrics",
                             {"name": name, "value": value, "step": step,
                              "source": source})
        return data

    def list_metrics(self, key, name=None, limit=200):
        data, _ = self._call("GET", f"/api/issues/{key}/metrics",
                             params={"name": name, "limit": limit})
        return data

    def latest_metrics(self, key):
        data, _ = self._call("GET", f"/api/issues/{key}/metrics",
                             params={"latest": "1"})
        return data

    def recent_events(self, since, limit=500):
        data, _ = self._call("GET", "/api/events",
                             params={"since": since, "limit": limit})
        return [Event.from_dict(e) for e in data]

    def metric_trend(self, key, name, window=10):
        data, _ = self._call("GET", f"/api/issues/{key}/metrics/trend",
                             params={"name": name, "window": window})
        return data

    def set_target(self, key, metric, op, value, note=None, actor=None):
        data, _ = self._call("POST", f"/api/issues/{key}/targets",
                             {"metric": metric, "op": op, "value": value,
                              "note": note, "actor": actor or self.config.actor})
        return data

    def remove_target(self, key, metric):
        data, _ = self._call("DELETE", f"/api/issues/{key}/targets",
                             {"metric": metric})
        return data

    def list_targets(self, key=None):
        data, _ = self._call("GET", "/api/targets", params={"issue": key})
        return data

    def evaluate_targets(self, key=None):
        return self.list_targets(key)

    def set_params(self, key, params, source=None, actor=None):
        data, _ = self._call("POST", f"/api/issues/{key}/params",
                             {"params": params, "source": source,
                              "actor": actor or self.config.actor})
        return data

    def list_params(self, key):
        data, _ = self._call("GET", f"/api/issues/{key}/params")
        return data

    def remove_param(self, key, name):
        data, _ = self._call("DELETE", f"/api/issues/{key}/params", {"name": name})
        return data

    def heartbeat(self, key, ref="run", **kw):
        data, _ = self._call("POST", f"/api/issues/{key}/heartbeat",
                             {"ref": ref, **kw})
        return data

    def scan(self, key=None, record_metrics=True):
        data, _ = self._call("POST", "/api/scan",
                             {"issue": key, "record_metrics": record_metrics})
        return data

    def backup(self, dest=None, keep=14):
        data, _ = self._call("POST", "/api/backup", {"dest": dest, "keep": keep})
        return data

    def workflow(self):
        data, _ = self._call("GET", "/api/workflow")
        return data


def token_for(api_url, explicit=None):
    """Find the API token: explicit, then env, then the shared token file."""
    if explicit:
        return explicit
    env = os.environ.get("TAM_API_TOKEN")
    if env:
        return env.strip()
    path = os.environ.get("TAM_API_TOKEN_FILE")
    if not path:
        home = os.environ.get("TAM_HOME")
        if home:
            path = str(Path(home) / "data" / "api_token")
    if path and Path(path).exists():
        return Path(path).read_text().strip()
    return None


def from_env(api_url=None, actor=None, token=None):
    actor = actor or os.environ.get("TAM_ACTOR")
    url = api_url or os.environ.get("TAM_API_URL")
    if not url:
        raise ValidationError("no API URL: pass --api or set TAM_API_URL")
    return RemoteService(url, token=token_for(url, token), actor=actor)
