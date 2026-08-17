"""Command-line adapter.

The primary agent surface: no daemon to keep alive, so any session can drive it
immediately. Every read command accepts `--json` and emits a stable envelope --
that, not the human table, is the contract for programmatic callers.

This module parses, calls core, and formats. It contains no workflow rules.
"""

import argparse
import json
import os
import secrets
import socket
import sys

from . import config as config_mod
from . import db as db_mod
from . import digest as digest_mod
from .clock import expand_date, today
from .core import Service, allowed_from
from .core import queue as queue_mod
from .core.query import IssueFilter, SORT_COLUMNS
from .errors import TamError, ValidationError
from .models import ISSUE_TYPES, PRIORITIES, STATUSES


class Out:
    """A command result: machine `data` plus the human rendering of it."""

    def __init__(self, data, text=None, meta=None):
        self.data = data
        self.text = text
        self.meta = meta or {}


# ---------------------------------------------------------------- presentation


def table(headers, rows):
    if not rows:
        return "  (none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append(
            "  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def issue_rows(issues, tz):
    now = today(tz).isoformat()
    rows = []
    for i in issues:
        due = i.due_date or ""
        if due and due < now and i.is_open:
            due += " !"
        rows.append([
            i.key, i.priority, i.status, i.type, due,
            i.assignee or "", (i.title[:60] + "…") if len(i.title) > 61 else i.title,
        ])
    return rows


def render_issue_list(issues, tz):
    return table(
        ["KEY", "PRI", "STATUS", "TYPE", "DUE", "ASSIGNEE", "TITLE"],
        issue_rows(issues, tz),
    )


def render_issue_detail(svc, issue, comments=None, links=None, history=None, kids=None):
    out = [
        f"{issue.key}  {issue.title}",
        "",
        f"  status    {issue.status}   (next: {', '.join(allowed_from(issue.status)) or 'none'})",
        f"  priority  {issue.priority}",
        f"  type      {issue.type}",
        f"  project   {issue.project_key}",
    ]
    for label, value in (
        ("assignee", issue.assignee), ("reporter", issue.reporter),
        ("due", issue.due_date), ("parent", issue.parent_key),
        ("labels", " ".join(issue.labels) if issue.labels else None),
        ("ref", issue.external_ref),
    ):
        if value:
            out.append(f"  {label:<9} {value}")
    out.append(f"  created   {issue.created_at}")
    out.append(f"  updated   {issue.updated_at}")
    if issue.closed_at:
        out.append(f"  closed    {issue.closed_at}")
    if issue.body:
        out += ["", "  " + issue.body.replace("\n", "\n  ")]
    if kids:
        out += ["", "subtasks:", render_issue_list(kids, svc.config.timezone)]
    if links:
        out += ["", "links:"] + [
            f"  {l.type:<14} {l.to_key:<10} [{l.to_status}] {l.to_title}" for l in links
        ]
    if comments:
        out += ["", "comments:"] + [
            f"  [{c.created_at}] {c.author}: {c.body}" for c in comments
        ]
    if history:
        out += ["", "history:"] + [f"  {_event_line(e)}" for e in history]
    return "\n".join(out)


def _event_line(e):
    if e.kind == "transitioned":
        text = f"{e.old_value} -> {e.new_value}"
    elif e.kind == "updated":
        text = f"{e.field_name}: {e.old_value!r} -> {e.new_value!r}"
    elif e.kind in ("linked", "unlinked"):
        text = f"{e.field_name} {e.new_value or e.old_value}"
    elif e.kind == "commented":
        text = (e.note or "")[:60]
    else:
        text = e.note or e.new_value or ""
    note = f"  ({e.note})" if e.note and e.kind == "transitioned" else ""
    return f"{e.at}  {e.actor:<12} {e.kind:<13} {text}{note}"


# -------------------------------------------------------------------- plumbing


def emit(args, out):
    if getattr(args, "json", False):
        payload = {"ok": True, "data": out.data}
        if out.meta:
            payload["meta"] = out.meta
        print(json.dumps(payload, indent=2, default=str))
    elif not getattr(args, "quiet", False) and out.text:
        print(out.text)


def fail(args, err):
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": err.to_dict()}, indent=2))
    else:
        print(f"error: {err.message}", file=sys.stderr)
    return err.exit_code


def build_filter(args, svc):
    tz = svc.config.timezone
    return IssueFilter(
        project=args.project,
        status=tuple(args.status or ()),
        priority=tuple(args.priority or ()),
        type=tuple(args.type or ()),
        assignee=args.assignee,
        labels=tuple(args.label or ()),
        text=args.text,
        due_before=expand_date(args.due_before, tz),
        due_after=expand_date(args.due_after, tz),
        due_on=expand_date(args.due_on, tz),
        overdue=args.overdue,
        stale_days=args.stale,
        parent=args.parent,
        include_closed=args.include_closed,
        sort=args.sort,
        order=args.order,
        limit=args.limit,
        offset=args.offset,
    )


# -------------------------------------------------------------------- handlers


def cmd_init(svc, args):
    cfg = svc.config
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if args.project_key:
        cfg.default_project = args.project_key.upper()
    # If someone initialises onto a network filesystem anyway, record which host
    # owns direct file access, so every other host is refused and pointed at the
    # API rather than losing writes. Local disk is the supported layout and
    # leaves db_host unset -- there, the filesystem enforces this by itself.
    shared = db_mod.is_network_fs(cfg.db_path)
    if shared and not cfg.db_host:
        cfg.db_host = socket.gethostname()
    cfg.save()

    if not cfg.token_path.exists():
        cfg.token_path.write_text(secrets.token_urlsafe(32) + "\n")
        cfg.token_path.chmod(0o600)

    created = False
    try:
        project = svc.get_project(cfg.default_project)
    except TamError:
        project = svc.create_project(cfg.default_project, args.project_name)
        created = True

    cfg.digest_path.mkdir(parents=True, exist_ok=True)
    data = {
        "root": str(cfg.root),
        "database": str(cfg.db_path),
        "config": str(cfg.config_path),
        "project": project.to_dict(),
        "project_created": created,
        "api_token_path": str(cfg.token_path),
    }
    lines = [
        f"initialised {cfg.root}",
        f"  database  {cfg.db_path}",
        f"  config    {cfg.config_path}",
        f"  digests   {cfg.digest_path}",
        f"  project   {project.key} ({'created' if created else 'existing'})",
        f"  api token {cfg.token_path}",
    ]
    if shared:
        data["db_host"] = cfg.db_host
        data["filesystem"] = db_mod.filesystem_type(cfg.db_path)
        lines += [
            f"  shared fs {data['filesystem']} — direct access restricted to "
            f"{cfg.db_host}",
            "            other hosts must use the API (README: Cluster access)",
        ]
    return Out(data, "\n".join(lines))


def cmd_project_list(svc, args):
    projects = svc.list_projects(include_archived=args.all)
    return Out(
        [p.to_dict() for p in projects],
        table(
            ["KEY", "NAME", "ISSUES", "ARCHIVED"],
            [[p.key, p.name, p.issue_seq, p.archived_at or ""] for p in projects],
        ),
        {"count": len(projects)},
    )


def cmd_project_create(svc, args):
    p = svc.create_project(args.key, args.name, args.description or "")
    return Out(p.to_dict(), f"created project {p.key}")


def cmd_project_show(svc, args):
    p = svc.get_project(args.key)
    s = svc.stats(project=p.key)
    return Out({**p.to_dict(), "stats": s},
               f"{p.key}  {p.name}\n  issues {s['total']} (open {s['open']})")


def cmd_project_archive(svc, args):
    p = svc.archive_project(args.key, unarchive=args.unarchive)
    verb = "unarchived" if args.unarchive else "archived"
    return Out(p.to_dict(), f"{verb} project {p.key}")


def cmd_issue_create(svc, args):
    issue = svc.create_issue(
        title=args.title, body=args.body or "", type=args.type, status=args.status,
        priority=args.priority, assignee=args.assignee, due_date=expand_date(
            args.due, svc.config.timezone),
        parent=args.parent, labels=args.label or (), project=args.project,
        external_ref=args.ref, reason=args.reason,
    )
    return Out(issue.to_dict(), f"{issue.key}  {issue.title}")


def cmd_issue_show(svc, args):
    issue = svc.get_issue(args.key)
    show_all = args.all
    comments = svc.list_comments(issue.key) if (args.comments or show_all) else None
    links = svc.list_links(issue.key) if (args.links or show_all) else None
    history = svc.history(issue.key) if (args.history or show_all) else None
    kids = svc.children(issue.key)
    data = issue.to_dict()
    data["subtasks"] = [k.to_dict() for k in kids]
    if comments is not None:
        data["comments"] = [c.to_dict() for c in comments]
    if links is not None:
        data["links"] = [l.to_dict() for l in links]
    if history is not None:
        data["history"] = [e.to_dict() for e in history]
    data["next_statuses"] = allowed_from(issue.status)
    return Out(data, render_issue_detail(svc, issue, comments, links, history, kids))


def cmd_issue_list(svc, args):
    issues = svc.list_issues(build_filter(args, svc))
    return Out(
        [i.to_dict() for i in issues],
        render_issue_list(issues, svc.config.timezone),
        {"count": len(issues)},
    )


def cmd_issue_update(svc, args):
    fields = {}
    for name in ("title", "body", "type", "priority", "assignee", "ref"):
        value = getattr(args, name)
        if value is not None:
            fields["external_ref" if name == "ref" else name] = value
    if args.clear_due:
        fields["due_date"] = None
    elif args.due is not None:
        fields["due_date"] = expand_date(args.due, svc.config.timezone)
    if args.clear_assignee:
        fields["assignee"] = None
    if args.label is not None:
        fields["labels"] = args.label
    if args.parent is not None:
        fields["parent"] = args.parent or None
    if not fields:
        raise ValidationError("no fields given to update")
    issue = svc.update_issue(args.key, **fields)
    return Out(issue.to_dict(), f"updated {issue.key}")


def cmd_issue_move(svc, args):
    issue = svc.transition(args.key, args.status, reason=args.reason, force=args.force)
    return Out(issue.to_dict(), f"{issue.key} -> {issue.status}")


def cmd_issue_delete(svc, args):
    if not args.yes:
        raise ValidationError("refusing to delete without --yes")
    result = svc.delete_issue(args.key)
    text = f"deleted {result['key']}"
    if result["orphaned_subtasks"]:
        text += f" (orphaned subtasks: {', '.join(result['orphaned_subtasks'])})"
    return Out(result, text)


def cmd_issue_history(svc, args):
    events = svc.history(args.key)
    return Out([e.to_dict() for e in events],
               "\n".join(_event_line(e) for e in events) or "  (no history)",
               {"count": len(events)})


def cmd_issue_children(svc, args):
    kids = svc.children(args.key)
    return Out([k.to_dict() for k in kids],
               render_issue_list(kids, svc.config.timezone), {"count": len(kids)})


def cmd_comment_add(svc, args):
    c = svc.add_comment(args.key, args.message)
    return Out(c.to_dict(), f"commented on {c.issue_key}")


def cmd_comment_list(svc, args):
    comments = svc.list_comments(args.key)
    return Out([c.to_dict() for c in comments],
               "\n".join(f"  [{c.created_at}] {c.author}: {c.body}" for c in comments)
               or "  (no comments)",
               {"count": len(comments)})


def cmd_label_add(svc, args):
    issue = svc.add_labels(args.key, args.labels)
    return Out(issue.to_dict(), f"{issue.key} labels: {' '.join(issue.labels)}")


def cmd_label_rm(svc, args):
    issue = svc.remove_labels(args.key, args.labels)
    return Out(issue.to_dict(), f"{issue.key} labels: {' '.join(issue.labels) or '(none)'}")


def cmd_label_list(svc, args):
    labels = svc.list_labels()
    return Out(labels, table(["LABEL", "ISSUES"],
                             [[l["name"], l["count"]] for l in labels]))


LINK_FLAGS = {"blocks": "blocks", "blocked_by": "blocked_by",
              "relates_to": "relates_to", "duplicates": "duplicates"}


def _link_type(args):
    for flag, link_type in LINK_FLAGS.items():
        if getattr(args, flag, None):
            return link_type, getattr(args, flag)
    raise ValidationError(
        "specify one of --blocks, --blocked-by, --relates-to, --duplicates")


def cmd_link_add(svc, args):
    link_type, target = _link_type(args)
    links = svc.add_link(args.key, target, link_type)
    return Out([l.to_dict() for l in links], f"{args.key} {link_type} {target}")


def cmd_link_rm(svc, args):
    links = svc.remove_link(args.key, args.target, args.type)
    return Out([l.to_dict() for l in links],
               f"removed {args.type} between {args.key} and {args.target}")


def cmd_link_list(svc, args):
    links = svc.list_links(args.key)
    return Out([l.to_dict() for l in links],
               table(["TYPE", "KEY", "STATUS", "TITLE"],
                     [[l.type, l.to_key, l.to_status, l.to_title] for l in links]))


def cmd_digest(svc, args):
    payload = svc.digest(expand_date(args.date, svc.config.timezone), write=args.write)
    text = digest_mod.render_markdown(payload)
    if args.write:
        where = getattr(svc.config, "api_url", None) or svc.config.digest_path
        text += f"\nsaved to {where}"
    return Out(payload, text)


def cmd_next(svc, args):
    """What to start next: the todo queue, gated on each lane's WIP headroom."""
    pull = svc.next_up(wip_limit=args.wip_limit)
    lines = []

    ready = pull["ready"]
    can = [r for r in ready if r["pullable"]]
    if can:
        lines.append(f"start now ({len(can)}):")
        for r in can:
            lane = r["lane"] if r["lane"] != queue_mod.NO_EPIC else "-"
            lines.append(f"  {r['key']:<10} {r['priority']}  {lane:<8} {r['title']}")
    queued = [r for r in ready if not r["pullable"]]
    if queued:
        lines.append("")
        lines.append(f"queued behind a full lane ({len(queued)}):")
        for r in queued:
            lane = r["lane"] if r["lane"] != queue_mod.NO_EPIC else "-"
            lines.append(f"  {r['key']:<10} {r['priority']}  {lane:<8} {r['title']}")

    if pull["blocked"]:
        lines.append("")
        lines.append(f"blocked ({len(pull['blocked'])}):")
        for b in pull["blocked"]:
            by = ", ".join(x["key"] for x in b["blocked_by"])
            lines.append(f"  {b['key']:<10} {b['priority']}  blocked by {by}")

    if pull["starved"]:
        lines.append("")
        lines.append("room but nothing queued:")
        for s in pull["starved"]:
            lane = s["lane"] if s["lane"] != queue_mod.NO_EPIC else "-"
            c = s["candidate"]
            tail = (f"{s['backlog_count']} in backlog, top {c['key']} {c['priority']} {c['title']}"
                    if c else "nothing in backlog either")
            lines.append(f"  {lane:<10} {s['headroom']} free · {tail}")

    if not lines:
        lines.append("nothing in todo — promote something from backlog first")

    lines.append("")
    lines.append("  ".join(
        f"{l['lane'] if l['lane'] != queue_mod.NO_EPIC else '-'} "
        f"{l['in_progress']}/{l['limit']}"
        for l in pull["lanes"]))
    return Out(pull, "\n".join(lines))


def cmd_review(svc, args):
    run_date = expand_date(args.date, svc.config.timezone) or today(
        svc.config.timezone).isoformat()
    if args.done is not None:
        svc.review(run_date, args.done or None)
        return Out({"date": run_date, "reviewed": True},
                   f"marked {run_date} digest reviewed")

    stored = svc.digest_run(run_date)
    payload = stored["payload"] if stored else svc.digest(run_date)
    reviewed_at = stored["reviewed_at"] if stored else None
    text = digest_mod.render_markdown(payload)
    text += (
        f"\n> already reviewed at {reviewed_at}\n"
        if reviewed_at
        else "\n> not yet reviewed — finish with: tam review --done \"notes\"\n"
    )
    return Out({**payload, "reviewed_at": reviewed_at, "persisted": bool(stored)}, text)


def cmd_tree(svc, args):
    """Parents with their subtasks nested underneath.

    `issue list` is deliberately flat, which makes a parent/subtask structure
    invisible. This is the view that shows the shape of the work.
    """
    issues = svc.list_issues(build_filter(args, svc))
    by_key = {i.key: i for i in issues}
    kids = {}
    roots = []
    for i in issues:
        # A subtask whose parent was filtered out still has to appear somewhere.
        if i.parent_key and i.parent_key in by_key:
            kids.setdefault(i.parent_key, []).append(i)
        else:
            roots.append(i)

    tz = svc.config.timezone
    now = today(tz).isoformat()
    lines = []

    def render(issue, depth):
        due = issue.due_date or ""
        if due and due < now and issue.is_open:
            due += " !"
        indent = "    " * depth + ("└ " if depth else "")
        lines.append(
            f"  {indent}{issue.key:<8} {issue.priority}  {issue.status:<11}  "
            f"{due:<12}  {issue.title}".rstrip()
        )
        for child in kids.get(issue.key, []):
            render(child, depth + 1)

    for root in roots:
        render(root, 0)
        if kids.get(root.key):
            lines.append("")

    data = [
        {**i.to_dict(), "subtasks": [c.to_dict() for c in kids.get(i.key, [])]}
        for i in roots
    ]
    return Out(data, "\n".join(lines).rstrip() or "  (none)",
               {"roots": len(roots), "total": len(issues)})


def cmd_nudge(svc, args):
    """One-line headline for shell login. Silent once the day is reviewed."""
    run_date = today(svc.config.timezone).isoformat()
    stored = svc.digest_run(run_date)
    if stored and stored["reviewed_at"]:
        return Out({"date": run_date, "reviewed": True, "text": None})

    payload = stored["payload"] if stored else svc.digest(run_date)
    urgent = len(payload["sections"]["overdue"]) + len(payload["sections"]["due_today"])
    if not urgent and not payload["sections"]["blocked"]:
        return Out({"date": run_date, "reviewed": False, "text": None})

    headline = f"TAM {run_date} — {payload['summary']}   (run: tam review)"
    text = headline if args.plain else f"\033[1;33mTAM {run_date}\033[0m — " \
        f"{payload['summary']}   (run: tam review)"
    return Out({"date": run_date, "reviewed": False, "text": headline}, text)



def cmd_watch_add(svc, args):
    watches = svc.add_watch(args.key, args.provider, args.ref, host=args.host,
                            label=args.label, config=args.config)
    return Out(watches, f"{args.key} now watches {args.provider}:{args.ref}")


def cmd_provider_list(svc, args):
    rows = svc.providers()
    return Out(rows, table(["PROVIDER", "REFERENCE", "DESCRIPTION"],
                           [[p["name"], p["ref_hint"], p["description"]]
                            for p in rows]), {"count": len(rows)})


def cmd_watch_list(svc, args):
    watches = svc.list_watches(args.key, args.provider)
    return Out(watches, table(
        ["ID", "ISSUE", "PROVIDER", "REF", "HOST", "STATE", "LAST SEEN"],
        [[w["id"], w["issue"], w["provider"], w["ref"][:40], w["host"] or "",
          w["state"] or "-", (w["last_seen"] or "")[:19]] for w in watches]),
        {"count": len(watches)})


def cmd_watch_rm(svc, args):
    return Out(svc.remove_watch(args.id), f"removed watch {args.id}")


def cmd_scan(svc, args):
    result = svc.scan(key=args.key, record_metrics=not args.no_metrics)
    lines = []
    for r in result["results"]:
        mark = "*" if r["changed"] else " "
        extra = "  ".join(f"{k}={v}" for k, v in (r.get("metrics") or {}).items())
        lines.append(f"  {mark} {r['issue']:<8} "
                     f"{r['provider']}:{r['ref'][:26]:<26} "
                     f"{str(r['state']):<10} {extra}".rstrip())
    if result.get("commented"):
        lines.append("")
        lines.append(f"  {len(result['commented'])} state change(s) "
                     f"commented on issues")
    if result.get("attention"):
        lines.append(f"  {len(result['attention'])} need attention: "
                     + ", ".join(f"{r['issue']} ({r['state']})"
                                 for r in result["attention"]))
    return Out(result, "\n".join(lines) or "  (no watches configured)",
               {"watches": result["watches"], "changed": len(result["changed"]),
                "attention": len(result.get("attention", []))})


def cmd_metric_add(svc, args):
    m = svc.record_metric(args.key, args.name, args.value, step=args.step,
                          source=args.source)
    return Out(m, f"{args.key} {args.name}={args.value}"
                  + (f" @step {args.step}" if args.step is not None else ""))


def cmd_metric_list(svc, args):
    points = svc.list_metrics(args.key, name=args.name, limit=args.limit or 200)
    return Out(points, table(["NAME", "VALUE", "STEP", "AT", "SOURCE"],
                             [[p["name"], p["value"], p["step"] if p["step"] is not None
                               else "", p["at"][:19], p["source"] or ""]
                              for p in points]), {"count": len(points)})


def cmd_metric_trend(svc, args):
    t = svc.metric_trend(args.key, args.name, window=args.window)
    if not t:
        return Out(None, f"  not enough data for {args.name} on {args.key}")
    lines = [
        f"  {args.key} {t['name']} over last {t['points']} points: "
        f"{t['first']:.4f} -> {t['last']:.4f}  ({t['delta']:+.4f})  "
        f"{t['verdict'].upper()}",
        f"  mean {t['mean']:.4f}  stdev {t['stdev']:.4f}  "
        f"range {t['min']:.4f} .. {t['max']:.4f}",
    ]
    if not t["flat"]:
        lines.append(f"  fitted change over window {t['change_over_window']:+.4f}")
    elif t["noise_dominated"]:
        # The endpoints move but the fit cannot support it -- say so, because
        # the raw delta is exactly what would mislead someone.
        lines.append(f"  endpoints differ by {t['delta']:+.4f}, but that is within "
                     f"the scatter — no trend the data supports")
    return Out(t, "\n".join(lines))


def cmd_target_set(svc, args):
    targets = svc.set_target(args.key, args.metric, args.op, args.value,
                             note=args.note)
    return Out(targets, f"{args.key}: {args.metric} {args.op} {args.value}")


def cmd_target_rm(svc, args):
    return Out(svc.remove_target(args.key, args.metric),
               f"removed target {args.metric} from {args.key}")


def cmd_target_list(svc, args):
    rows = svc.evaluate_targets(args.key)
    return Out(rows, table(
        ["ISSUE", "METRIC", "TARGET", "ACTUAL", "STATUS", "GAP"],
        [[r["issue"], r["metric"], f"{r['op']} {r['value']}",
          "-" if r["actual"] is None else f"{r['actual']:.4f}",
          r["status"], "" if r["gap"] in (None, 0.0) else f"{r['gap']:+.4f}"]
         for r in rows]), {"count": len(rows)})


def cmd_param_set(svc, args):
    params = {}
    for pair in args.pairs:
        if "=" not in pair:
            raise ValidationError(f"expected name=value, got {pair!r}")
        name, _, raw = pair.partition("=")
        params[name.strip()] = _coerce(raw)
    stored = svc.set_params(args.key, params, source=args.source)
    return Out(stored, f"{args.key}: set {', '.join(sorted(params))}")


def _coerce(raw):
    """Turn a CLI string into the narrowest sensible type."""
    text = raw.strip()
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def cmd_param_list(svc, args):
    params = svc.list_params(args.key)
    return Out(params, table(["PARAM", "VALUE"],
                             [[k, v] for k, v in sorted(params.items())]),
               {"count": len(params)})


def cmd_param_rm(svc, args):
    return Out(svc.remove_param(args.key, args.name),
               f"removed param {args.name} from {args.key}")


def cmd_heartbeat(svc, args):
    metrics = {}
    for pair in args.metric or []:
        name, _, raw = pair.partition("=")
        metrics[name.strip()] = float(raw)
    result = svc.heartbeat(args.key, args.ref, metrics=metrics or None,
                           step=args.step, final_state=args.state,
                           note=args.note, source=args.source,
                           timeout_seconds=args.timeout)
    extra = f" ({args.state})" if args.state else ""
    return Out(result, f"{args.key} heartbeat {args.ref} #{result['beats']}{extra}")


def cmd_backup(svc, args):
    r = svc.backup(dest=args.to, keep=args.keep)
    text = (f"  backed up {r['issues']} issues -> {r['path']}\n"
            f"  {r['bytes']} bytes, integrity {r['integrity']}, keeping {r['kept']}")
    if r["pruned"]:
        text += f"\n  pruned {len(r['pruned'])} old backup(s)"
    return Out(r, text)


def cmd_stats(svc, args):
    s = svc.stats(project=args.project)
    text = "\n".join([
        f"  total {s['total']}   open {s['open']}   closed {s['closed']}",
        "  " + "  ".join(f"{k} {v}" for k, v in s["by_status"].items()),
        "  " + "  ".join(f"{k} {v}" for k, v in s["open_by_priority"].items()),
    ])
    return Out(s, text)


def cmd_config_show(svc, args):
    data = svc.config.to_dict()
    return Out(data, "\n".join(f"  {k:<17} {v}" for k, v in data.items()))


# ---------------------------------------------------------------------- parser


def add_filter_args(p):
    p.add_argument("--project")
    p.add_argument("--status", nargs="+", choices=STATUSES)
    p.add_argument("--priority", nargs="+", choices=PRIORITIES)
    p.add_argument("--type", nargs="+", choices=ISSUE_TYPES)
    p.add_argument("--assignee", help="name, or 'unassigned'")
    p.add_argument("--label", nargs="+", help="all given labels must be present")
    p.add_argument("--text", help="search title and body")
    p.add_argument("--due-before", dest="due_before", metavar="DATE")
    p.add_argument("--due-after", dest="due_after", metavar="DATE")
    p.add_argument("--due-on", dest="due_on", metavar="DATE")
    p.add_argument("--overdue", action="store_true")
    p.add_argument("--stale", type=int, metavar="DAYS",
                   help="no activity for DAYS days")
    p.add_argument("--parent", metavar="KEY")
    p.add_argument("--include-closed", dest="include_closed", action="store_true")
    p.add_argument("--sort", default="priority", choices=sorted(SORT_COLUMNS))
    p.add_argument("--order", default="asc", choices=["asc", "desc"])
    p.add_argument("--limit", type=int)
    p.add_argument("--offset", type=int, default=0)


def build_parser():
    # Global flags are attached to every subparser so they work in either
    # position. They must use SUPPRESS: without it, a subparser re-applies its
    # own default and silently wipes a value given before the subcommand --
    # `tam --db other.db issue create` would write to the wrong database.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="override the database file only")
    common.add_argument("--home", default=argparse.SUPPRESS,
                        help="override the install root (database, config, digests)")
    common.add_argument("--api", default=argparse.SUPPRESS,
                        metavar="URL",
                        help="work through a remote TAM API instead of the local "
                             "database (or set TAM_API_URL)")
    common.add_argument("--actor", default=argparse.SUPPRESS,
                        help="attribute changes to this actor")
    common.add_argument("-q", "--quiet", action="store_true",
                        default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="tam", parents=[common],
        description="TAM — task automanager",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def leaf(parent, name, func, **kw):
        p = parent.add_parser(name, parents=[common], **kw)
        p.set_defaults(func=func)
        return p

    p = leaf(sub, "init", cmd_init, help="create the database, config and first project")
    p.add_argument("--project-key", dest="project_key")
    p.add_argument("--project-name", dest="project_name")

    # -- project
    proj = sub.add_parser("project", help="manage projects").add_subparsers(
        dest="subcmd", required=True)
    leaf(proj, "list", cmd_project_list).add_argument("--all", action="store_true")
    p = leaf(proj, "create", cmd_project_create)
    p.add_argument("key")
    p.add_argument("--name")
    p.add_argument("--description")
    leaf(proj, "show", cmd_project_show).add_argument("key")
    p = leaf(proj, "archive", cmd_project_archive)
    p.add_argument("key")
    p.add_argument("--unarchive", action="store_true")

    # -- issue
    issue = sub.add_parser("issue", help="manage issues").add_subparsers(
        dest="subcmd", required=True)

    p = leaf(issue, "create", cmd_issue_create)
    p.add_argument("-t", "--title", required=True)
    p.add_argument("-b", "--body")
    p.add_argument("--type", default="task", choices=ISSUE_TYPES)
    p.add_argument("-p", "--priority", default="p2", choices=PRIORITIES)
    p.add_argument("-s", "--status", default="backlog", choices=STATUSES)
    p.add_argument("-a", "--assignee")
    p.add_argument("-d", "--due", metavar="DATE",
                   help="YYYY-MM-DD, today, tomorrow, +3d")
    p.add_argument("--parent", metavar="KEY")
    p.add_argument("-l", "--label", nargs="+")
    p.add_argument("--project")
    p.add_argument("--ref", help="external URL or path")
    p.add_argument("--reason", help="required when creating as blocked")

    p = leaf(issue, "show", cmd_issue_show)
    p.add_argument("key")
    p.add_argument("--comments", action="store_true")
    p.add_argument("--history", action="store_true")
    p.add_argument("--links", action="store_true")
    p.add_argument("--all", action="store_true", help="comments, links and history")

    add_filter_args(leaf(issue, "list", cmd_issue_list))
    add_filter_args(leaf(issue, "tree", cmd_tree,
                         help="parents with their subtasks nested"))

    p = leaf(issue, "update", cmd_issue_update)
    p.add_argument("key")
    p.add_argument("-t", "--title")
    p.add_argument("-b", "--body")
    p.add_argument("--type", choices=ISSUE_TYPES)
    p.add_argument("-p", "--priority", choices=PRIORITIES)
    p.add_argument("-a", "--assignee")
    p.add_argument("-d", "--due", metavar="DATE")
    p.add_argument("--clear-due", dest="clear_due", action="store_true")
    p.add_argument("--clear-assignee", dest="clear_assignee", action="store_true")
    p.add_argument("-l", "--label", nargs="*", help="replaces all labels")
    p.add_argument("--parent", metavar="KEY", help="empty string detaches")
    p.add_argument("--ref")

    p = leaf(issue, "move", cmd_issue_move, help="change status")
    p.add_argument("key")
    p.add_argument("status", choices=STATUSES)
    p.add_argument("--reason", help="required when moving to blocked")
    p.add_argument("--force", action="store_true", help="close over open subtasks")

    p = leaf(issue, "delete", cmd_issue_delete)
    p.add_argument("key")
    p.add_argument("--yes", action="store_true", required=False)

    leaf(issue, "history", cmd_issue_history).add_argument("key")
    leaf(issue, "children", cmd_issue_children).add_argument("key")

    # -- comment
    comment = sub.add_parser("comment", help="issue comments").add_subparsers(
        dest="subcmd", required=True)
    p = leaf(comment, "add", cmd_comment_add)
    p.add_argument("key")
    p.add_argument("-m", "--message", required=True)
    leaf(comment, "list", cmd_comment_list).add_argument("key")

    # -- label
    label = sub.add_parser("label", help="issue labels").add_subparsers(
        dest="subcmd", required=True)
    p = leaf(label, "add", cmd_label_add)
    p.add_argument("key")
    p.add_argument("labels", nargs="+")
    p = leaf(label, "rm", cmd_label_rm)
    p.add_argument("key")
    p.add_argument("labels", nargs="+")
    leaf(label, "list", cmd_label_list)

    # -- link
    link = sub.add_parser("link", help="issue links").add_subparsers(
        dest="subcmd", required=True)
    p = leaf(link, "add", cmd_link_add)
    p.add_argument("key")
    p.add_argument("--blocks", metavar="KEY")
    p.add_argument("--blocked-by", dest="blocked_by", metavar="KEY")
    p.add_argument("--relates-to", dest="relates_to", metavar="KEY")
    p.add_argument("--duplicates", metavar="KEY")
    p = leaf(link, "rm", cmd_link_rm)
    p.add_argument("key")
    p.add_argument("target")
    p.add_argument("--type", required=True, choices=sorted(LINK_FLAGS))
    leaf(link, "list", cmd_link_list).add_argument("key")

    # -- digest / review / stats / config
    p = leaf(sub, "next", cmd_next,
             help="what to start next: the todo queue, gated on WIP headroom")
    p.add_argument("--wip-limit", dest="wip_limit", type=int, metavar="N",
                   help="override the configured WIP limit for this view")

    p = leaf(sub, "digest", cmd_digest, help="build the daily digest")
    p.add_argument("--date", metavar="DATE")
    p.add_argument("--write", action="store_true", help="persist and write files")

    p = leaf(sub, "review", cmd_review, help="show or close out the daily review")
    p.add_argument("--date", metavar="DATE")
    p.add_argument("--done", nargs="?", const="", metavar="NOTES",
                   help="mark today's digest reviewed")

    p = leaf(sub, "nudge", cmd_nudge,
             help="one-line headline for shell login; silent when nothing is urgent")
    p.add_argument("--plain", action="store_true", help="no ANSI colour")

    # -- watch / scan / metric / target / backup
    watch = sub.add_parser("watch", help="bind issues to jobs, processes, paths"
                           ).add_subparsers(dest="subcmd", required=True)
    p = leaf(watch, "add", cmd_watch_add)
    p.add_argument("key")
    p.add_argument("provider", help="see: tam provider list")
    p.add_argument("ref", help="job id, path, pattern, URL, command ...")
    p.add_argument("--host", help="where to look, for host-based providers")
    p.add_argument("--label")
    p.add_argument("--config", metavar="JSON",
                   help="provider settings, e.g. '{\"parser\":\"yolo\"}'")
    p = leaf(watch, "list", cmd_watch_list)
    p.add_argument("key", nargs="?")
    p.add_argument("--provider")

    prov = sub.add_parser("provider", help="available watch providers"
                          ).add_subparsers(dest="subcmd", required=True)
    leaf(prov, "list", cmd_provider_list)
    leaf(watch, "rm", cmd_watch_rm).add_argument("id", type=int)

    p = leaf(sub, "scan", cmd_scan, help="probe every watch and record what changed")
    p.add_argument("key", nargs="?", help="limit to one issue")
    p.add_argument("--no-metrics", dest="no_metrics", action="store_true")

    metric = sub.add_parser("metric", help="numeric observations").add_subparsers(
        dest="subcmd", required=True)
    p = leaf(metric, "add", cmd_metric_add)
    p.add_argument("key")
    p.add_argument("name")
    p.add_argument("value", type=float)
    p.add_argument("--step", type=int)
    p.add_argument("--source")
    p = leaf(metric, "list", cmd_metric_list)
    p.add_argument("key")
    p.add_argument("--name")
    p.add_argument("--limit", type=int)
    p = leaf(metric, "trend", cmd_metric_trend)
    p.add_argument("key")
    p.add_argument("name")
    p.add_argument("--window", type=int, default=10)

    target = sub.add_parser("target", help="acceptance criteria").add_subparsers(
        dest="subcmd", required=True)
    p = leaf(target, "set", cmd_target_set)
    p.add_argument("key")
    p.add_argument("metric")
    p.add_argument("op", choices=[">=", ">", "<=", "<", "=="])
    p.add_argument("value", type=float)
    p.add_argument("--note")
    p = leaf(target, "rm", cmd_target_rm)
    p.add_argument("key")
    p.add_argument("metric")
    leaf(target, "list", cmd_target_list).add_argument("key", nargs="?")

    param = sub.add_parser("param", help="run configuration").add_subparsers(
        dest="subcmd", required=True)
    p = leaf(param, "set", cmd_param_set)
    p.add_argument("key")
    p.add_argument("pairs", nargs="+", metavar="NAME=VALUE")
    p.add_argument("--source")
    leaf(param, "list", cmd_param_list).add_argument("key")
    p = leaf(param, "rm", cmd_param_rm)
    p.add_argument("key")
    p.add_argument("name")

    p = leaf(sub, "heartbeat", cmd_heartbeat,
             help="report liveness from a job TAM cannot observe directly")
    p.add_argument("key")
    p.add_argument("ref", nargs="?", default="run")
    p.add_argument("--metric", nargs="+", metavar="NAME=VALUE")
    p.add_argument("--step", type=int)
    p.add_argument("--state", choices=["running", "succeeded", "failed",
                                       "stopped"],
                   help="report a final state and stop expecting beats")
    p.add_argument("--note")
    p.add_argument("--source")
    p.add_argument("--timeout", type=int, metavar="SECONDS",
                   help="silence after this long means stopped (default 900)")

    p = leaf(sub, "backup", cmd_backup, help="consistent snapshot of the database")
    p.add_argument("--to", metavar="DIR")
    p.add_argument("--keep", type=int, default=14)

    leaf(sub, "stats", cmd_stats).add_argument("--project")

    cfg = sub.add_parser("config", help="configuration").add_subparsers(
        dest="subcmd", required=True)
    leaf(cfg, "show", cmd_config_show)

    return parser


GLOBAL_DEFAULTS = {"json": False, "db": None, "home": None, "actor": None,
                   "api": None, "quiet": False}


def apply_global_defaults(args):
    """Fill in globals that SUPPRESS left unset.

    They cannot be supplied via `set_defaults`: that mutates the shared action
    objects installed by `parents=[common]`, turning SUPPRESS back into a real
    default on every subparser -- which is exactly the clobbering SUPPRESS is
    here to prevent.
    """
    for name, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    return args


def main(argv=None):
    parser = build_parser()
    args = apply_global_defaults(parser.parse_args(argv))

    api_url = args.api or os.environ.get("TAM_API_URL")
    config = None if api_url else config_mod.load(
        db_path=args.db, actor=args.actor, root=args.home)
    svc = None
    try:
        if api_url:
            if args.cmd == "init":
                raise ValidationError(
                    "init creates a database and must run on the owner host, "
                    "not through --api")
            from .remote import from_env
            svc = from_env(api_url, actor=args.actor)
            emit(args, args.func(svc, args))
            return 0
        # Constructed inside the guard: opening the database can itself fail
        # (e.g. the shared-database owner check), and that must be reported as
        # a normal error with an exit code, not an unhandled traceback.
        svc = Service(config)
        emit(args, args.func(svc, args))
        return 0
    except TamError as err:
        return fail(args, err)
    except BrokenPipeError:
        return 0
    finally:
        if svc is not None:
            svc.close()


if __name__ == "__main__":
    sys.exit(main())
