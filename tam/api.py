"""HTTP adapter.

JSON API over `http.server`. Like the CLI it parses, calls core, and serialises
-- no rules live here.

Security posture: loopback by default, gated on a bearer token generated at
`tam init`. Serving a cluster means setting `api_host` to 0.0.0.0 deliberately,
which is what every other node uses to reach a database that only this host can
open. There are no per-user permissions: the token is the entire boundary, so
one holder can do anything any other holder can. That is an accepted trade for a
single-user system on a trusted network -- `make_server` warns on startup when
the bind is not loopback, and the Host allowlist below still applies.
"""

import hmac
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from pathlib import Path

from . import __version__
from . import config as config_mod
from . import digest as digest_mod
from .clock import expand_date, today
from .core import Service, allowed_from
from .core.query import filter_from_params
from .errors import AuthError, NotFoundError, TamError, ValidationError
from .models import (
    CLOSED_STATUSES,
    ISSUE_TYPES,
    OPEN_STATUSES,
    PRIORITIES,
    PRIORITY_LABEL,
    STATUSES,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Agent-facing documentation is served from the repo rather than copied to
# shared storage, for the same reason the code is not published there: a second
# copy is a copy that drifts. A node reads its instructions from the same host
# it reads its issues from, so the two can never disagree about which version
# is current.
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

ISSUE_FIELDS = ("title", "body", "type", "priority", "assignee", "due_date",
                "external_ref", "labels", "parent")


# ------------------------------------------------------------------- handlers


def h_health(svc, m, params, body):
    """Liveness probe and version. The only unauthenticated route."""
    return 200, {"status": "ok", "version": __version__}, None


def h_list_projects(svc, m, params, body):
    """List projects; ?all=1 includes archived ones."""
    projects = svc.list_projects(include_archived=params.get("all") == ["1"])
    return 200, [p.to_dict() for p in projects], {"count": len(projects)}


def h_create_project(svc, m, params, body):
    """Create a project. Body: key, name, description."""
    p = svc.create_project(
        body.get("key"), body.get("name"), body.get("description", "")
    )
    return 201, p.to_dict(), None


def h_list_issues(svc, m, params, body):
    """Search and filter issues. Closed excluded unless asked for."""
    f = filter_from_params(params, svc.config.timezone)
    issues = svc.list_issues(f)
    return 200, [i.to_dict() for i in issues], {"count": len(issues)}


def h_create_issue(svc, m, params, body):
    """Create an issue. Body: title (required) plus any issue field."""
    kw = {k: body[k] for k in ISSUE_FIELDS if k in body}
    if "due_date" in kw:
        kw["due_date"] = expand_date(kw["due_date"], svc.config.timezone)
    if "labels" in kw and isinstance(kw["labels"], str):
        kw["labels"] = [x for x in kw["labels"].split(",") if x]
    for extra in ("status", "project", "reason", "reporter", "actor"):
        if extra in body:
            kw[extra] = body[extra]
    issue = svc.create_issue(**kw)
    return 201, issue.to_dict(), None


def h_get_issue(svc, m, params, body):
    """One issue with subtasks, links and allowed next statuses."""
    key = m.group("key")
    issue = svc.get_issue(key)
    data = issue.to_dict()
    data["subtasks"] = [k.to_dict() for k in svc.children(key)]
    data["links"] = [l.to_dict() for l in svc.list_links(key)]
    data["next_statuses"] = allowed_from(issue.status)
    if params.get("comments") == ["1"]:
        data["comments"] = [c.to_dict() for c in svc.list_comments(key)]
    if params.get("history") == ["1"]:
        data["history"] = [e.to_dict() for e in svc.history(key)]
    return 200, data, None


def h_patch_issue(svc, m, params, body):
    """Update issue fields. Status is rejected here - use /transition."""
    kw = {k: body[k] for k in ISSUE_FIELDS if k in body}
    if not kw:
        raise ValidationError("no updatable fields in request body")
    if "due_date" in kw and kw["due_date"]:
        kw["due_date"] = expand_date(kw["due_date"], svc.config.timezone)
    if "status" in body:
        raise ValidationError(
            "use POST /api/issues/{key}/transition to change status", field="status"
        )
    if body.get("actor"):
        kw["actor"] = body["actor"]
    return 200, svc.update_issue(m.group("key"), **kw).to_dict(), None


def h_delete_issue(svc, m, params, body):
    """Delete an issue. Its audit history survives."""
    return 200, svc.delete_issue(m.group("key"), actor=(body or {}).get("actor")), None


def h_transition(svc, m, params, body):
    """Change status. Enforces the workflow matrix and its guards."""
    if "status" not in body:
        raise ValidationError("transition requires a status", field="status")
    issue = svc.transition(
        m.group("key"), body["status"],
        reason=body.get("reason"), force=bool(body.get("force")),
        actor=body.get("actor"),
    )
    return 200, issue.to_dict(), None


def h_list_comments(svc, m, params, body):
    """Comments on an issue, oldest first."""
    comments = svc.list_comments(m.group("key"))
    return 200, [c.to_dict() for c in comments], {"count": len(comments)}


def h_add_comment(svc, m, params, body):
    """Add a comment. Counts as activity for staleness."""
    c = svc.add_comment(m.group("key"), body.get("body"), author=body.get("author"))
    return 201, c.to_dict(), None


def h_history(svc, m, params, body):
    """Full audit trail for an issue."""
    events = svc.history(m.group("key"))
    return 200, [e.to_dict() for e in events], {"count": len(events)}


def h_list_links(svc, m, params, body):
    """Links from an issue, with the target's title and status."""
    links = svc.list_links(m.group("key"))
    return 200, [l.to_dict() for l in links], {"count": len(links)}


def h_add_link(svc, m, params, body):
    """Link two issues; the inverse link is created automatically."""
    links = svc.add_link(m.group("key"), body.get("to"), body.get("type"),
                         actor=body.get("actor"))
    return 201, [l.to_dict() for l in links], None


def h_remove_link(svc, m, params, body):
    """Remove a link and its inverse."""
    links = svc.remove_link(m.group("key"), body.get("to"), body.get("type"),
                            actor=body.get("actor"))
    return 200, [l.to_dict() for l in links], None


def h_digest(svc, m, params, body):
    """Build the daily digest; ?write=1 persists it."""
    run_date = expand_date((params.get("date") or [None])[0], svc.config.timezone)
    return 200, svc.digest(run_date, write=params.get("write") == ["1"]), None


def h_review(svc, m, params, body):
    """Mark a date's digest reviewed, with optional notes."""
    run_date = expand_date(m.group("date"), svc.config.timezone)
    return 200, svc.review(run_date, (body or {}).get("notes")), None


def h_stats(svc, m, params, body):
    """Issue counts by status and priority."""
    return 200, svc.stats(project=(params.get("project") or [None])[0]), None


def h_labels(svc, m, params, body):
    """All labels with usage counts."""
    return 200, svc.list_labels(), None


def h_workflow(svc, m, params, body):
    """Publish the transition matrix so clients never hard-code a copy of it.

    The board UI uses this to dim illegal drop targets, and the remote CLI uses
    it for timezone and limits. The server still enforces every rule -- this
    only drives affordances.
    """
    return 200, svc.workflow(), None


def h_get_project(svc, m, params, body):
    """One project with its issue counts."""
    project = svc.get_project(m.group("pkey"))
    return 200, {**project.to_dict(), "stats": svc.stats(project=project.key)}, None


def h_archive_project(svc, m, params, body):
    """Archive a project, or revive it by passing unarchive true."""
    unarchive = bool((body or {}).get("unarchive"))
    return 200, svc.archive_project(m.group("pkey"), unarchive).to_dict(), None


def h_digest_run(svc, m, params, body):
    """The stored digest run, including whether it has been reviewed."""
    run = svc.digest_run(m.group("date"))
    if run is None:
        raise NotFoundError(f"no digest run for {m.group('date')}", date=m.group("date"))
    return 200, run, None



def h_list_watches(svc, m, params, body):
    """Watches, optionally filtered by issue or provider."""
    watches = svc.list_watches((params.get("issue") or [None])[0],
                               (params.get("provider") or [None])[0])
    return 200, watches, {"count": len(watches)}


def h_add_watch(svc, m, params, body):
    """Bind an issue to something observable. Body: provider, ref, config."""
    return 201, svc.add_watch(m.group("key"),
                              body.get("provider") or body.get("kind"),
                              body.get("ref"), host=body.get("host"),
                              label=body.get("label"), config=body.get("config"),
                              actor=body.get("actor")), None


def h_remove_watch(svc, m, params, body):
    """Remove a watch."""
    return 200, svc.remove_watch(int(m.group("wid"))), None


def h_scan(svc, m, params, body):
    """Probe every watch now; records metrics and comments on changes."""
    return 200, svc.scan(key=(body or {}).get("issue"),
                         record_metrics=(body or {}).get("record_metrics", True)), None


def h_list_metrics(svc, m, params, body):
    """Metric points for an issue; ?latest=1 for current values."""
    if params.get("latest") == ["1"]:
        return 200, svc.latest_metrics(m.group("key")), None
    points = svc.list_metrics(m.group("key"), (params.get("name") or [None])[0],
                              int((params.get("limit") or [200])[0]))
    return 200, points, {"count": len(points)}


def h_add_metric(svc, m, params, body):
    """Record one metric observation."""
    return 201, svc.record_metric(m.group("key"), body.get("name"),
                                  body.get("value"), step=body.get("step"),
                                  source=body.get("source")), None


def h_metric_trend(svc, m, params, body):
    """Trend verdict over a window, testing slope against scatter."""
    return 200, svc.metric_trend(m.group("key"), (params.get("name") or [""])[0],
                                 int((params.get("window") or [10])[0])), None


def h_list_targets(svc, m, params, body):
    """Targets with their status - met, not met, or no data."""
    rows = svc.evaluate_targets((params.get("issue") or [None])[0])
    return 200, rows, {"count": len(rows)}


def h_set_target(svc, m, params, body):
    """Set an acceptance criterion. Body: metric, op, value."""
    return 201, svc.set_target(m.group("key"), body.get("metric"), body.get("op"),
                               body.get("value"), note=body.get("note"),
                               actor=body.get("actor")), None


def h_remove_target(svc, m, params, body):
    """Remove a target."""
    return 200, svc.remove_target(m.group("key"), (body or {}).get("metric")), None


def h_list_params(svc, m, params, body):
    """Run configuration recorded for an issue."""
    return 200, svc.list_params(m.group("key")), None


def h_set_params(svc, m, params, body):
    """Record run configuration. Re-setting a name overwrites it."""
    payload = body.get("params") if "params" in body else body
    return 201, svc.set_params(m.group("key"), payload,
                               source=(body or {}).get("source"),
                               actor=(body or {}).get("actor")), None


def h_remove_param(svc, m, params, body):
    """Remove one parameter."""
    return 200, svc.remove_param(m.group("key"), (body or {}).get("name")), None


def h_heartbeat(svc, m, params, body):
    """Report liveness from a job TAM cannot observe directly."""
    body = body or {}
    return 201, svc.heartbeat(
        m.group("key"), body.get("ref", "run"), metrics=body.get("metrics"),
        step=body.get("step"), params=body.get("params"),
        final_state=body.get("final_state"), note=body.get("note"),
        source=body.get("source"), timeout_seconds=body.get("timeout_seconds")), None


def h_recent_events(svc, m, params, body):
    """Change feed over the audit log. Poll with ?since=<iso>."""
    since = (params.get("since") or [""])[0]
    limit = int((params.get("limit") or [500])[0])
    events = svc.recent_events(since, limit)
    return 200, [e.to_dict() for e in events], {"count": len(events)}


def h_board(svc, m, params, body):
    """Everything the board UI needs in one call: issues, metrics, targets, watches."""
    f = filter_from_params(params, svc.config.timezone)
    issues = svc.list_issues(f)
    keys = [i.key for i in issues]

    # One pass over the observation tables rather than a request per card.
    latest, series = {}, {}
    for key in keys:
        vals = svc.latest_metrics(key)
        if vals:
            latest[key] = vals
            # short trailing series per metric, for sparklines
            spark = {}
            for name in vals:
                pts = svc.list_metrics(key, name=name, limit=30)
                if len(pts) > 1:
                    spark[name] = [p["value"] for p in pts]
            if spark:
                series[key] = spark

    watches = {}
    for w in svc.list_watches():
        watches.setdefault(w["issue"], []).append(
            {"provider": w["provider"], "ref": w["ref"], "state": w["state"],
             "native_state": w["native_state"], "last_seen": w["last_seen"]})

    targets = {}
    for t in svc.evaluate_targets():
        targets.setdefault(t["issue"], []).append(t)

    return 200, {
        "issues": [i.to_dict() for i in issues],
        "metrics": latest, "series": series,
        "watches": watches, "targets": targets,
        "workflow": svc.workflow(),
        "stats": svc.stats(),
    }, {"count": len(issues)}


def h_providers(svc, m, params, body):
    """Watch providers available, including locally added ones."""
    return 200, svc.providers(), None


def h_backup(svc, m, params, body):
    """Write a consistent database snapshot and prune old ones."""
    return 201, svc.backup(dest=(body or {}).get("dest"),
                           keep=(body or {}).get("keep", 14)), None


KEY = r"(?P<key>[A-Za-z][A-Za-z0-9]{1,9}-\d+)"
DATE = r"(?P<date>\d{4}-\d{2}-\d{2})"
PKEY = r"(?P<pkey>[A-Za-z][A-Za-z0-9]{1,9})"

ROUTES = [
    ("GET", r"^/health$", h_health, False),
    ("GET", r"^/api/projects$", h_list_projects, True),
    ("POST", r"^/api/projects$", h_create_project, True),
    ("GET", r"^/api/issues$", h_list_issues, True),
    ("POST", r"^/api/issues$", h_create_issue, True),
    ("GET", rf"^/api/issues/{KEY}$", h_get_issue, True),
    ("PATCH", rf"^/api/issues/{KEY}$", h_patch_issue, True),
    ("DELETE", rf"^/api/issues/{KEY}$", h_delete_issue, True),
    ("POST", rf"^/api/issues/{KEY}/transition$", h_transition, True),
    ("GET", rf"^/api/issues/{KEY}/comments$", h_list_comments, True),
    ("POST", rf"^/api/issues/{KEY}/comments$", h_add_comment, True),
    ("GET", rf"^/api/issues/{KEY}/history$", h_history, True),
    ("GET", rf"^/api/issues/{KEY}/links$", h_list_links, True),
    ("POST", rf"^/api/issues/{KEY}/links$", h_add_link, True),
    ("DELETE", rf"^/api/issues/{KEY}/links$", h_remove_link, True),
    ("GET", r"^/api/digest$", h_digest, True),
    ("POST", rf"^/api/digest/{DATE}/review$", h_review, True),
    ("GET", r"^/api/stats$", h_stats, True),
    ("GET", r"^/api/labels$", h_labels, True),
    ("GET", r"^/api/workflow$", h_workflow, True),
    ("GET", rf"^/api/projects/{PKEY}$", h_get_project, True),
    ("POST", rf"^/api/projects/{PKEY}/archive$", h_archive_project, True),
    ("GET", rf"^/api/digest/{DATE}/run$", h_digest_run, True),
    ("GET", r"^/api/watches$", h_list_watches, True),
    ("POST", rf"^/api/issues/{KEY}/watches$", h_add_watch, True),
    ("DELETE", r"^/api/watches/(?P<wid>\d+)$", h_remove_watch, True),
    ("POST", r"^/api/scan$", h_scan, True),
    ("GET", rf"^/api/issues/{KEY}/metrics$", h_list_metrics, True),
    ("POST", rf"^/api/issues/{KEY}/metrics$", h_add_metric, True),
    ("GET", rf"^/api/issues/{KEY}/metrics/trend$", h_metric_trend, True),
    ("GET", r"^/api/targets$", h_list_targets, True),
    ("POST", rf"^/api/issues/{KEY}/targets$", h_set_target, True),
    ("DELETE", rf"^/api/issues/{KEY}/targets$", h_remove_target, True),
    ("POST", r"^/api/backup$", h_backup, True),
    ("GET", r"^/api/providers$", h_providers, True),
    ("GET", r"^/api/events$", h_recent_events, True),
    ("GET", r"^/api/board$", h_board, True),
    ("GET", rf"^/api/issues/{KEY}/params$", h_list_params, True),
    ("POST", rf"^/api/issues/{KEY}/params$", h_set_params, True),
    ("DELETE", rf"^/api/issues/{KEY}/params$", h_remove_param, True),
    ("POST", rf"^/api/issues/{KEY}/heartbeat$", h_heartbeat, True),
]
COMPILED = [(method, re.compile(pattern), fn, auth)
            for method, pattern, fn, auth in ROUTES]


# --------------------------------------------------------------------- server


def read_token(config):
    path = config.token_path
    if not path.exists():
        return None
    return path.read_text().strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "tam"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        if self.server.tam_verbose:
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def _send(self, status, payload):
        self._send_raw(status, json.dumps(payload, indent=2, default=str).encode(),
                       "application/json")

    def _send_raw(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # No Access-Control-Allow-Origin anywhere: a page on another origin must
        # not be able to read this API's responses, token included.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _host_allowed(self):
        """Reject requests whose Host is not one we serve on.

        Defends against DNS rebinding: a hostile domain resolving to one of our
        addresses would otherwise reach this server as a same-origin page. The
        allowlist is built once at startup, so serving the cluster does not mean
        accepting arbitrary Host headers.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in self.server.tam_allowed_hosts

    def _serve_board(self):
        """Serve the board UI with the API token injected.

        The token is embedded rather than fetched: the browser has no other way
        to obtain it. This is safe only because no CORS headers are sent, so a
        cross-origin page cannot read this response.
        """
        path = STATIC_DIR / "board.html"
        if not path.exists():
            return self._send(404, {"ok": False, "error": {
                "code": "not_found", "message": "board UI is not installed"}})
        html = path.read_text().replace(
            "__TAM_TOKEN__", json.dumps(self.server.tam_token or "")
        )
        self._send_raw(200, html.encode(), "text/html; charset=utf-8")

    def _serve_docs(self, path):
        """Serve `docs/*.md` as plain text: GET /docs, GET /docs/ARGUS.md.

        Read-only and confined to DOCS_DIR by resolving the candidate and
        checking its parent, so a traversal attempt lands outside and is
        refused rather than reaching into the checkout.
        """
        name = path[len("/docs"):].lstrip("/")
        if not name:
            listing = sorted(p.name for p in DOCS_DIR.glob("*.md"))
            return self._send(200, {"ok": True, "data": listing})
        candidate = (DOCS_DIR / name).resolve()
        if candidate.parent != DOCS_DIR.resolve() or candidate.suffix != ".md" \
                or not candidate.is_file():
            return self._send(404, {"ok": False, "error": {
                "code": "not_found", "message": f"no such document: {name}"}})
        self._send_raw(200, candidate.read_bytes(),
                       "text/markdown; charset=utf-8")

    def _authorised(self):
        expected = self.server.tam_token
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            presented.strip(), expected
        )

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ValidationError(f"malformed JSON body: {exc}")
        if not isinstance(parsed, dict):
            raise ValidationError("request body must be a JSON object")
        return parsed

    # -- dispatch ---------------------------------------------------------
    def _dispatch(self, method):
        if not self._host_allowed():
            return self._send(403, {"ok": False, "error": {
                "code": "forbidden", "message": "unrecognised Host header"}})

        url = urlparse(self.path)
        path = unquote(url.path).rstrip("/") or "/"
        params = parse_qs(url.query)

        if path == "/" and method == "GET":
            return self._serve_board()

        if method == "GET" and (path == "/docs" or path.startswith("/docs/")):
            if not self._authorised():
                err = AuthError("missing or invalid bearer token")
                return self._send(err.http_status,
                                  {"ok": False, "error": err.to_dict()})
            return self._serve_docs(path)

        matched_path = False
        for route_method, pattern, fn, needs_auth in COMPILED:
            m = pattern.match(path)
            if not m:
                continue
            matched_path = True
            if route_method != method:
                continue
            try:
                if needs_auth and not self._authorised():
                    raise AuthError("missing or invalid bearer token")
                body = self._body() if method in ("POST", "PATCH", "PUT", "DELETE") else {}
                svc = Service(self.server.tam_config)
                try:
                    status, data, meta = fn(svc, m, params, body)
                finally:
                    svc.close()
                payload = {"ok": True, "data": data}
                if meta:
                    payload["meta"] = meta
                return self._send(status, payload)
            except TamError as err:
                return self._send(err.http_status, {"ok": False, "error": err.to_dict()})
            except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
                sys.stderr.write(f"unhandled: {exc!r}\n")
                return self._send(500, {"ok": False, "error": {
                    "code": "internal", "message": str(exc)}})

        if matched_path:
            return self._send(405, {"ok": False, "error": {
                "code": "method_not_allowed", "message": f"{method} not allowed on {path}"}})
        return self._send(404, {"ok": False, "error": {
            "code": "not_found", "message": f"no route for {path}"}})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


def make_server(config=None, host=None, port=None, verbose=False):
    config = config or config_mod.load()
    Service(config).close()          # apply migrations, and fail fast if this
                                     # host may not open the database directly
    server = ThreadingHTTPServer(
        (host or config.api_host, port if port is not None else config.api_port),
        Handler,
    )
    server.daemon_threads = True
    server.tam_config = config
    server.tam_token = read_token(config)
    server.tam_verbose = verbose
    server.tam_allowed_hosts = allowed_hosts(
        server.server_address[0], getattr(config, "api_allowed_hosts", ()) or ())
    return server


def local_addresses():
    """Best-effort list of addresses this machine answers on.

    `gethostname` is not enough: on this cluster the hostname resolves to a
    different address than the interface actually serving traffic. The UDP
    trick reads the primary outbound address without sending a packet.
    """
    import socket as _socket

    found = set()
    try:
        name = _socket.gethostname()
        found.update({name, _socket.getfqdn(name)})
        found.update(_socket.gethostbyname_ex(name)[2])
    except OSError:
        pass
    try:
        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 1))     # TEST-NET-1, never routed
            found.add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    return found


def allowed_hosts(bound_ip, extra=()):
    """Host header values this server will answer to.

    Kept to a fixed set computed at startup: serving the cluster must not mean
    accepting an arbitrary Host, or the DNS-rebinding defence is gone.
    """
    hosts = {"127.0.0.1", "localhost", "::1", ""}
    if bound_ip and bound_ip not in ("0.0.0.0", "::"):
        hosts.add(bound_ip)
    hosts |= local_addresses()
    hosts |= {h for h in (extra or ()) if h}
    return {h for h in hosts if h is not None}


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(prog="tam-api", description="TAM HTTP API")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--db")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    config = config_mod.load(db_path=args.db)
    server = make_server(config, args.host, args.port, args.verbose)
    host, port = server.server_address[:2]
    token_note = "token required" if server.tam_token else "NO TOKEN — unauthenticated"
    sys.stderr.write(f"tam-api listening on http://{host}:{port} ({token_note})\n")
    if host not in ("127.0.0.1", "::1", "localhost"):
        sys.stderr.write(
            "warning: bound to a non-loopback address; this API is designed for "
            "local single-user use\n"
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nshutting down\n")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
