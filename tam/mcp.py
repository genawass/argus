"""MCP adapter: JSON-RPC 2.0 over newline-delimited stdio.

Gives a Claude Code session native `tam_*` tools with no Bash quoting in the
way. Implements `initialize`, `notifications/initialized`, `tools/list` and
`tools/call`.

The one rule that matters here: **stdout carries protocol frames and nothing
else.** Every diagnostic goes to stderr. A stray print to stdout corrupts the
stream and the client silently drops the server.
"""

import json
import os
import sys

from . import __version__
from . import config as config_mod
from . import digest as digest_mod
from .clock import expand_date
from .core import Service, allowed_from
from .core.query import filter_from_params
from .errors import TamError, ValidationError
from .models import ISSUE_TYPES, PRIORITIES, STATUSES

PROTOCOL_VERSION = "2025-06-18"

STR = {"type": "string"}


def _enum(values, desc):
    return {"type": "string", "enum": list(values), "description": desc}


FILTER_PROPS = {
    "project": STR,
    "status": {"type": "array", "items": _enum(STATUSES, "status"),
               "description": "match any of these statuses"},
    "priority": {"type": "array", "items": _enum(PRIORITIES, "priority")},
    "type": {"type": "array", "items": _enum(ISSUE_TYPES, "issue type")},
    "assignee": {"type": "string", "description": "name, or 'unassigned'"},
    "labels": {"type": "array", "items": STR,
               "description": "every listed label must be present"},
    "text": {"type": "string", "description": "search title and body"},
    "due_before": {"type": "string", "description": "YYYY-MM-DD, today, +3d"},
    "due_after": STR,
    "due_on": STR,
    "overdue": {"type": "boolean"},
    "stale_days": {"type": "integer", "description": "no activity for N days"},
    "parent": {"type": "string", "description": "subtasks of this issue key"},
    "has_parent": {"type": "boolean"},
    "is_blocked": {"type": "boolean",
                   "description": "blocked status, or has an open blocker"},
    "include_closed": {"type": "boolean"},
    "sort": _enum(["priority", "due_date", "updated_at", "created_at", "status",
                   "title", "key"], "sort field"),
    "order": _enum(["asc", "desc"], "sort direction"),
    "limit": {"type": "integer"},
    "offset": {"type": "integer"},
    "brief": {"type": "boolean",
              "description": "return a reduced projection (no body) to save tokens"},
}


# ----------------------------------------------------------------- tool impls


def t_create_issue(svc, a):
    kw = {k: a[k] for k in ("title", "body", "type", "status", "priority", "assignee",
                            "parent", "project", "external_ref", "reason", "labels")
          if k in a}
    if "due_date" in a:
        kw["due_date"] = expand_date(a["due_date"], svc.config.timezone)
    return svc.create_issue(**kw).to_dict()


def t_list_issues(svc, a):
    args = dict(a)
    brief = bool(args.pop("brief", False))
    issues = svc.list_issues(filter_from_params(args, svc.config.timezone))
    return {"count": len(issues),
            "issues": [i.to_dict(brief=brief) for i in issues]}


def t_get_issue(svc, a):
    key = a["key"]
    issue = svc.get_issue(key)
    data = issue.to_dict()
    data["next_statuses"] = allowed_from(issue.status)
    data["subtasks"] = [k.to_dict() for k in svc.children(key)]
    data["links"] = [l.to_dict() for l in svc.list_links(key)]
    if a.get("comments"):
        data["comments"] = [c.to_dict() for c in svc.list_comments(key)]
    if a.get("history"):
        data["history"] = [e.to_dict() for e in svc.history(key)]
    return data


def t_update_issue(svc, a):
    kw = {k: a[k] for k in ("title", "body", "type", "priority", "assignee",
                            "external_ref", "labels", "parent") if k in a}
    if "due_date" in a:
        kw["due_date"] = expand_date(a["due_date"], svc.config.timezone) \
            if a["due_date"] else None
    if not kw:
        raise ValidationError("no fields given to update")
    return svc.update_issue(a["key"], **kw).to_dict()


def t_transition_issue(svc, a):
    return svc.transition(
        a["key"], a["status"], reason=a.get("reason"), force=bool(a.get("force"))
    ).to_dict()


def t_add_comment(svc, a):
    return svc.add_comment(a["key"], a["body"]).to_dict()


def t_link_issues(svc, a):
    links = svc.add_link(a["key"], a["to"], a["type"])
    return {"links": [l.to_dict() for l in links]}


def t_issue_history(svc, a):
    return {"events": [e.to_dict() for e in svc.history(a["key"])]}


def t_daily_digest(svc, a):
    payload = digest_mod.build(svc, expand_date(a.get("date"), svc.config.timezone))
    if a.get("write"):
        digest_mod.save(svc, payload)
    return payload


def t_stats(svc, a):
    return svc.stats(project=a.get("project"))


def t_watch_issue(svc, a):
    return {"watches": svc.add_watch(a["key"], a["provider"], a["ref"],
                                     host=a.get("host"), label=a.get("label"),
                                     config=a.get("config"))}


def t_set_params(svc, a):
    return {"params": svc.set_params(a["key"], a["params"], source=a.get("source"))}


def t_get_experiment(svc, a):
    """Everything about an issue as an experiment: config, results, liveness."""
    key = a["key"]
    return {
        "issue": svc.get_issue(key).to_dict(),
        "params": svc.list_params(key),
        "metrics": svc.latest_metrics(key),
        "targets": svc.evaluate_targets(key),
        "watches": svc.list_watches(key),
    }


def t_list_providers(svc, a):
    return {"providers": svc.providers()}


def t_scan_cluster(svc, a):
    return svc.scan(key=a.get("key"), record_metrics=a.get("record_metrics", True))


def t_record_metric(svc, a):
    return svc.record_metric(a["key"], a["name"], a["value"],
                             step=a.get("step"), source=a.get("source"))


def t_metric_history(svc, a):
    out = {"points": svc.list_metrics(a["key"], a.get("name"),
                                      a.get("limit", 50))}
    if a.get("name"):
        out["trend"] = svc.metric_trend(a["key"], a["name"], a.get("window", 10))
    return out


def t_set_target(svc, a):
    return {"targets": svc.set_target(a["key"], a["metric"], a["op"], a["value"],
                                      note=a.get("note"))}


def t_check_targets(svc, a):
    return {"targets": svc.evaluate_targets(a.get("key"))}


TOOLS = [
    {
        "name": "tam_create_issue",
        "description": "Create an issue. Returns the created issue including its key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "required, short summary"},
                "body": STR,
                "type": _enum(ISSUE_TYPES, "issue type (default task)"),
                "status": _enum(STATUSES, "initial status (default backlog)"),
                "priority": _enum(PRIORITIES, "p0 critical .. p3 low (default p2)"),
                "assignee": STR,
                "due_date": {"type": "string",
                             "description": "YYYY-MM-DD, today, tomorrow, +3d"},
                "parent": {"type": "string", "description": "parent issue key"},
                "labels": {"type": "array", "items": STR},
                "project": {"type": "string", "description": "defaults to config"},
                "external_ref": STR,
                "reason": {"type": "string",
                           "description": "required when status is blocked"},
            },
            "required": ["title"],
        },
        "fn": t_create_issue,
    },
    {
        "name": "tam_list_issues",
        "description": ("Search and filter issues. Closed issues are excluded "
                        "unless include_closed or an explicit status is given."),
        "inputSchema": {"type": "object", "properties": FILTER_PROPS},
        "fn": t_list_issues,
    },
    {
        "name": "tam_get_issue",
        "description": "Fetch one issue with its subtasks, links and allowed next statuses.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "e.g. TAM-42"},
                "comments": {"type": "boolean"},
                "history": {"type": "boolean"},
            },
            "required": ["key"],
        },
        "fn": t_get_issue,
    },
    {
        "name": "tam_update_issue",
        "description": ("Update issue fields. Status cannot be changed here -- "
                        "use tam_transition_issue."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": STR, "title": STR, "body": STR,
                "type": _enum(ISSUE_TYPES, "issue type"),
                "priority": _enum(PRIORITIES, "priority"),
                "assignee": STR, "due_date": STR, "external_ref": STR,
                "parent": STR,
                "labels": {"type": "array", "items": STR,
                           "description": "replaces the full label set"},
            },
            "required": ["key"],
        },
        "fn": t_update_issue,
    },
    {
        "name": "tam_transition_issue",
        "description": ("Change an issue's status. Moving to blocked requires a "
                        "reason; closing a parent with open subtasks requires force."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": STR,
                "status": _enum(STATUSES, "target status"),
                "reason": STR,
                "force": {"type": "boolean",
                          "description": "close over open subtasks"},
            },
            "required": ["key", "status"],
        },
        "fn": t_transition_issue,
    },
    {
        "name": "tam_add_comment",
        "description": "Add a comment to an issue. Counts as activity for staleness.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": STR, "body": STR},
            "required": ["key", "body"],
        },
        "fn": t_add_comment,
    },
    {
        "name": "tam_link_issues",
        "description": "Link two issues. The inverse link is created automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": STR, "to": STR,
                "type": _enum(["blocks", "blocked_by", "relates_to", "duplicates"],
                              "link type"),
            },
            "required": ["key", "to", "type"],
        },
        "fn": t_link_issues,
    },
    {
        "name": "tam_issue_history",
        "description": "Full audit trail for an issue: who changed what, when.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": STR},
            "required": ["key"],
        },
        "fn": t_issue_history,
    },
    {
        "name": "tam_daily_digest",
        "description": ("Build the daily digest: overdue, due today, blocked, "
                        "stale, triage queue and counts."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "defaults to today"},
                "write": {"type": "boolean", "description": "persist the run"},
            },
        },
        "fn": t_daily_digest,
    },
    {
        "name": "tam_stats",
        "description": "Issue counts by status and priority.",
        "inputSchema": {"type": "object", "properties": {"project": STR}},
        "fn": t_stats,
    },
    {
        "name": "tam_watch_issue",
        "description": ("Bind an issue to something observable so scans keep it "
                        "current: a Slurm job id, a process pattern on a host, or "
                        "a directory that should be growing."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": STR,
                "provider": {"type": "string",
                             "description": "provider name; see tam_list_providers"},
                "ref": {"type": "string",
                        "description": "job id, path, pattern, URL or command"},
                "host": {"type": "string",
                         "description": "host to probe, for host-based providers"},
                "label": STR,
                "config": {"type": "object",
                           "description": "provider settings, e.g. {parser: yolo}"},
            },
            "required": ["key", "provider", "ref"],
        },
        "fn": t_watch_issue,
    },
    {
        "name": "tam_scan_cluster",
        "description": ("Probe every watch: Slurm job states, processes, output "
                        "directories. Records training metrics, and comments on "
                        "issues whose watched state changed. Read-only against "
                        "the cluster."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "limit to one issue"},
                "record_metrics": {"type": "boolean"},
            },
        },
        "fn": t_scan_cluster,
    },
    {
        "name": "tam_record_metric",
        "description": "Record a numeric observation (e.g. recall=0.803 at step 80).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": STR, "name": STR,
                "value": {"type": "number"},
                "step": {"type": "integer", "description": "epoch or iteration"},
                "source": STR,
            },
            "required": ["key", "name", "value"],
        },
        "fn": t_record_metric,
    },
    {
        "name": "tam_metric_history",
        "description": ("Metric points over time for an issue, plus a trend verdict "
                        "(rising / falling / flat) when a name is given."),
        "inputSchema": {
            "type": "object",
            "properties": {"key": STR, "name": STR,
                           "limit": {"type": "integer"},
                           "window": {"type": "integer"}},
            "required": ["key"],
        },
        "fn": t_metric_history,
    },
    {
        "name": "tam_set_target",
        "description": ("Set an acceptance criterion as data, e.g. recall >= 0.90, "
                        "so progress toward it can be evaluated automatically."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": STR, "metric": STR,
                "op": _enum([">=", ">", "<=", "<", "=="], "comparison"),
                "value": {"type": "number"}, "note": STR,
            },
            "required": ["key", "metric", "op", "value"],
        },
        "fn": t_set_target,
    },
    {
        "name": "tam_set_params",
        "description": ("Record run configuration (hyperparameters, dataset, "
                        "commit) so results can be traced back to their setup."),
        "inputSchema": {
            "type": "object",
            "properties": {"key": STR, "params": {"type": "object"},
                           "source": STR},
            "required": ["key", "params"],
        },
        "fn": t_set_params,
    },
    {
        "name": "tam_get_experiment",
        "description": ("One call for the whole picture of a piece of work: the "
                        "issue, its params, latest metrics, target status and "
                        "what is currently running for it."),
        "inputSchema": {
            "type": "object",
            "properties": {"key": STR},
            "required": ["key"],
        },
        "fn": t_get_experiment,
    },
    {
        "name": "tam_list_providers",
        "description": ("Watch providers available on this install, including any "
                        "added locally. Call before tam_watch_issue."),
        "inputSchema": {"type": "object", "properties": {}},
        "fn": t_list_providers,
    },
    {
        "name": "tam_check_targets",
        "description": ("Evaluate every target against the latest metric: met or "
                        "not, and by how much."),
        "inputSchema": {"type": "object", "properties": {"key": STR}},
        "fn": t_check_targets,
    },
]

BY_NAME = {t["name"]: t for t in TOOLS}


def tool_descriptors():
    return [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]


# ------------------------------------------------------------------- protocol


class Server:
    def __init__(self, config=None, out=None, log=None, api_url=None):
        self.api_url = api_url
        self.config = config or (None if api_url else config_mod.load())
        self.out = out or sys.stdout
        self.log = log or sys.stderr

    def _service(self):
        """Local database, or the API when this node may not open the file."""
        if self.api_url:
            from .remote import from_env
            return from_env(self.api_url)
        return Service(self.config)

    # -- JSON-RPC ---------------------------------------------------------
    def send(self, message):
        self.out.write(json.dumps(message) + "\n")
        self.out.flush()

    def reply(self, req_id, result):
        self.send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def error(self, req_id, code, message):
        self.send({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": code, "message": message}})

    # -- dispatch ---------------------------------------------------------
    def handle(self, message):
        method = message.get("method")
        req_id = message.get("id")

        if method == "initialize":
            return self.reply(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "tam", "version": __version__},
            })

        if method in ("notifications/initialized", "initialized"):
            return None                      # notification: no response

        if method == "ping":
            return self.reply(req_id, {})

        if method == "tools/list":
            return self.reply(req_id, {"tools": tool_descriptors()})

        if method == "tools/call":
            return self.call_tool(req_id, message.get("params") or {})

        if req_id is None:
            return None                      # unknown notification: ignore
        return self.error(req_id, -32601, f"method not found: {method}")

    def call_tool(self, req_id, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = BY_NAME.get(name)
        if not tool:
            return self.error(req_id, -32602, f"unknown tool: {name}")

        try:
            svc = self._service()
            try:
                result = tool["fn"](svc, args)
            finally:
                svc.close()
        except TamError as err:
            return self.reply(req_id, {
                "content": [{"type": "text",
                             "text": json.dumps({"ok": False, "error": err.to_dict()})}],
                "isError": True,
            })
        except KeyError as exc:
            return self.reply(req_id, {
                "content": [{"type": "text", "text": json.dumps(
                    {"ok": False, "error": {"code": "validation",
                                            "message": f"missing argument: {exc}"}})}],
                "isError": True,
            })
        except Exception as exc:  # noqa: BLE001 - report, never crash the stream
            self.log.write(f"tool {name} failed: {exc!r}\n")
            return self.reply(req_id, {
                "content": [{"type": "text", "text": json.dumps(
                    {"ok": False, "error": {"code": "internal", "message": str(exc)}})}],
                "isError": True,
            })

        return self.reply(req_id, {
            "content": [{"type": "text",
                         "text": json.dumps(result, indent=2, default=str)}],
            "structuredContent": result if isinstance(result, dict) else {"result": result},
        })

    # -- loop -------------------------------------------------------------
    def serve(self, stream=None):
        stream = stream or sys.stdin
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self.log.write(f"malformed frame: {exc}\n")
                self.send({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": "parse error"}})
                continue
            try:
                self.handle(message)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not kill us
                self.log.write(f"handler crashed: {exc!r}\n")
                if message.get("id") is not None:
                    self.error(message["id"], -32603, str(exc))


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(prog="tam-mcp", description="TAM MCP server")
    parser.add_argument("--db")
    parser.add_argument("--api", metavar="URL",
                        help="work through a remote TAM API (or set TAM_API_URL)")
    args = parser.parse_args(argv)

    api_url = args.api or os.environ.get("TAM_API_URL")
    try:
        if api_url:
            from .remote import from_env
            from_env(api_url).close()        # fail fast if unreachable
            Server(None, api_url=api_url).serve()
        else:
            config = config_mod.load(db_path=args.db)
            Service(config).close()          # migrate before any client request
            Server(config).serve()
    except TamError as err:
        # stdout carries protocol frames only, so startup errors go to stderr.
        sys.stderr.write(f"tam-mcp: {err.message}\n")
        return err.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
