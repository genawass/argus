"""Watches, metrics and targets -- the observable side of an issue.

Watches bind an issue to something real (a Slurm job, a process, a growing
directory). Metrics are numeric observations over time. Targets are acceptance
criteria expressed as data, so "Accuracy above 90" is something the system can
evaluate rather than prose a human has to judge.
"""

import json

from ..clock import utcnow
from ..db import tx
from ..errors import NotFoundError, ValidationError
from . import events
from .issues import _row

OPS = {
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
}


def _watch_dict(row):
    return {
        "id": row["id"],
        "issue": row["issue_key"] if "issue_key" in row.keys() else None,
        "provider": row["provider"],
        "ref": row["ref"],
        "host": row["host"],
        "label": row["label"],
        "config": json.loads(row["config"]) if row["config"] else None,
        "state": row["state"],
        "native_state": row["native_state"],
        "detail": json.loads(row["detail"]) if row["detail"] else None,
        "last_seen": row["last_seen"],
        "created_at": row["created_at"],
    }


# ------------------------------------------------------------------- watches


def add_watch(ctx, key, provider, ref, host=None, label=None, config=None,
              actor=None):
    """Bind an issue to something observable.

    `provider` is validated against the registry rather than a fixed list, so
    a new tool is a plugin rather than a schema change.
    """
    from .. import providers as provider_registry
    provider_registry.load_user_providers(ctx.config.data_dir / "providers")
    provider_registry.get(provider)          # raises if unknown

    ref = (ref or "").strip()
    if not ref:
        raise ValidationError("watch needs a reference", field="ref")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"config must be JSON: {exc}", field="config")
    actor = ctx.actor(actor)
    now = utcnow()
    with tx(ctx.conn):
        row = _row(ctx, key)
        ctx.conn.execute(
            "INSERT INTO watch(issue_id, provider, ref, host, label, config,"
            " created_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(issue_id, provider, ref) DO UPDATE SET"
            " host=excluded.host, label=excluded.label, config=excluded.config",
            (row["id"], provider, ref, host, label,
             json.dumps(config) if config else None, now),
        )
        events.record(conn=ctx.conn, issue_id=row["id"], issue_key=row["key"],
                      actor=actor, kind="updated", field="watch",
                      new=f"{provider}:{ref}", at=now)
    return list_watches(ctx, key)


def remove_watch(ctx, watch_id, actor=None):
    with tx(ctx.conn):
        row = ctx.conn.execute(
            "SELECT w.*, i.key AS issue_key FROM watch w "
            "JOIN issue i ON i.id = w.issue_id WHERE w.id=?", (watch_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"no watch {watch_id}", id=watch_id)
        ctx.conn.execute("DELETE FROM watch WHERE id=?", (watch_id,))
        events.record(conn=ctx.conn, issue_id=row["issue_id"],
                      issue_key=row["issue_key"], actor=ctx.actor(actor),
                      kind="updated", field="watch",
                      old=f"{row['provider']}:{row['ref']}")
    return {"id": watch_id, "removed": True}


def list_watches(ctx, key=None, provider=None):
    sql = ("SELECT w.*, i.key AS issue_key FROM watch w "
           "JOIN issue i ON i.id = w.issue_id")
    clauses, params = [], []
    if key:
        clauses.append("i.key = ?")
        params.append(key.strip().upper())
    if provider:
        clauses.append("w.provider = ?")
        params.append(provider)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY w.id"
    return [_watch_dict(r) for r in ctx.conn.execute(sql, params).fetchall()]


def update_watch_state(ctx, watch_id, state, native_state=None, detail=None):
    """Record what a scan observed. Returns the previous canonical state."""
    with tx(ctx.conn):
        row = ctx.conn.execute("SELECT state FROM watch WHERE id=?",
                               (watch_id,)).fetchone()
        if not row:
            raise NotFoundError(f"no watch {watch_id}", id=watch_id)
        ctx.conn.execute(
            "UPDATE watch SET state=?, native_state=?, detail=?, last_seen=?"
            " WHERE id=?",
            (state, native_state,
             json.dumps(detail) if detail is not None else None,
             utcnow(), watch_id),
        )
        return row["state"]


# ------------------------------------------------------------------- metrics


def record_metric(ctx, key, name, value, step=None, source=None, at=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"metric value must be numeric, got {value!r}",
                              field="value")
    # Step is an epoch/iteration counter, so it must be an integer. Coercing on
    # write also keeps a non-numeric value from reaching the board, which
    # interpolates it straight into a tooltip's innerHTML.
    if step is not None:
        try:
            step = int(step)
        except (TypeError, ValueError):
            raise ValidationError(f"metric step must be an integer, got {step!r}",
                                  field="step")
    name = (name or "").strip()
    if not name:
        raise ValidationError("metric needs a name", field="name")
    with tx(ctx.conn):
        row = _row(ctx, key)
        # Re-scanning the same log must not create duplicate points.
        ctx.conn.execute(
            "INSERT INTO metric(issue_id, name, value, step, source, at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(issue_id, name, step, source) WHERE step IS NOT NULL"
            " DO UPDATE SET value=excluded.value, at=excluded.at",
            (row["id"], name, value, step, source, at or utcnow()),
        )
    return {"issue": row["key"], "name": name, "value": value, "step": step}


def list_metrics(ctx, key, name=None, limit=200):
    row = _row(ctx, key)
    sql = "SELECT * FROM metric WHERE issue_id=?"
    params = [row["id"]]
    if name:
        sql += " AND name=?"
        params.append(name)
    sql += " ORDER BY at DESC, step DESC LIMIT ?"
    params.append(int(limit))
    rows = ctx.conn.execute(sql, params).fetchall()
    return [
        {"name": r["name"], "value": r["value"], "step": r["step"],
         "source": r["source"], "at": r["at"]}
        for r in reversed(rows)
    ]


def latest_metrics(ctx, key):
    """Most recent value of each metric on an issue.

    Ordered by (at, step, id) rather than `at` alone: timestamps have second
    resolution, so a batch recorded in one scan ties on `at` and grouping by
    time alone returns an arbitrary row from the tie.
    """
    row = _row(ctx, key)
    rows = ctx.conn.execute(
        "SELECT name, value, step, at FROM ("
        "  SELECT name, value, step, at, ROW_NUMBER() OVER ("
        "    PARTITION BY name ORDER BY at DESC, COALESCE(step,-1) DESC, id DESC"
        "  ) AS rn FROM metric WHERE issue_id=?"
        ") WHERE rn = 1 ORDER BY name",
        (row["id"],),
    ).fetchall()
    return {r["name"]: {"value": r["value"], "step": r["step"], "at": r["at"]}
            for r in rows}


def metric_trend(ctx, key, name, window=10):
    """Is this metric actually moving, or just noisy?

    Comparing the first and last point is not good enough: a metric
    oscillating around a fixed value produces an endpoint delta whose sign is
    arbitrary, and reading that as a trend can drive a real decision the wrong
    way (for example, whether to kill a training run that has converged).

    So fit a least-squares line over the window and compare the slope against
    its own standard error. A direction is reported only when the fit is
    stronger than the scatter around it; otherwise the verdict is "flat" and
    `noise_dominated` says why.
    """
    points = list_metrics(ctx, key, name=name, limit=window)
    if len(points) < 2:
        return None

    values = [p["value"] for p in points]
    n = len(values)
    mean_y = sum(values) / n
    spread = (sum((v - mean_y) ** 2 for v in values) / n) ** 0.5

    # Least-squares slope over evenly spaced indices.
    xs = list(range(n))
    mean_x = sum(xs) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    slope = (sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / sxx
             if sxx else 0.0)
    intercept = mean_y - slope * mean_x

    residuals = [y - (intercept + slope * x) for x, y in zip(xs, values)]
    dof = n - 2
    resid_sd = ((sum(r * r for r in residuals) / dof) ** 0.5) if dof > 0 else 0.0
    stderr = (resid_sd / (sxx ** 0.5)) if (sxx and resid_sd) else 0.0

    # |t| >= 2 is the usual rough bar for "not explainable by scatter alone".
    if slope == 0:
        t_stat, significant = 0.0, False
    elif stderr == 0:
        t_stat, significant = float("inf"), True      # a perfect line
    else:
        t_stat = slope / stderr
        significant = abs(t_stat) >= 2.0

    verdict = "flat" if not significant else ("rising" if slope > 0 else "falling")
    return {
        "name": name, "points": n,
        "first": values[0], "last": values[-1], "delta": values[-1] - values[0],
        "min": min(values), "max": max(values),
        "mean": mean_y, "stdev": spread,
        "slope_per_point": slope,
        "change_over_window": slope * (n - 1),
        "t_stat": None if t_stat == float("inf") else t_stat,
        "verdict": verdict,
        "flat": verdict == "flat",
        # True when the endpoints suggest movement the fit cannot support.
        "noise_dominated": verdict == "flat" and abs(values[-1] - values[0]) > 0,
    }


# ------------------------------------------------------------------- targets


def set_target(ctx, key, metric, op, value, note=None, actor=None):
    if op not in OPS:
        raise ValidationError(f"op must be one of {', '.join(OPS)}", field="op")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValidationError("target value must be numeric", field="value")
    now = utcnow()
    with tx(ctx.conn):
        row = _row(ctx, key)
        ctx.conn.execute(
            "INSERT INTO target(issue_id, metric, op, value, note, created_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(issue_id, metric) DO UPDATE SET"
            " op=excluded.op, value=excluded.value, note=excluded.note",
            (row["id"], metric, op, value, note, now),
        )
        events.record(conn=ctx.conn, issue_id=row["id"], issue_key=row["key"],
                      actor=ctx.actor(actor), kind="updated", field="target",
                      new=f"{metric} {op} {value}", at=now)
    return list_targets(ctx, key)


def remove_target(ctx, key, metric):
    with tx(ctx.conn):
        row = _row(ctx, key)
        cur = ctx.conn.execute("DELETE FROM target WHERE issue_id=? AND metric=?",
                               (row["id"], metric))
        if not cur.rowcount:
            raise NotFoundError(f"no target {metric} on {row['key']}", metric=metric)
    return {"issue": row["key"], "metric": metric, "removed": True}


def list_targets(ctx, key=None):
    sql = ("SELECT t.*, i.key AS issue_key FROM target t "
           "JOIN issue i ON i.id = t.issue_id")
    params = []
    if key:
        sql += " WHERE i.key = ?"
        params.append(key.strip().upper())
    sql += " ORDER BY i.key, t.metric"
    return [
        {"issue": r["issue_key"], "metric": r["metric"], "op": r["op"],
         "value": r["value"], "note": r["note"]}
        for r in ctx.conn.execute(sql, params).fetchall()
    ]


def _metric_with_parent_fallback(ctx, key, name):
    """Latest value of `name` on the issue, else on its parent.

    A training job is watched on the parent that owns it, while the acceptance
    criteria live on its subtasks ("Accuracy above 90" under "Model A"). Without this
    fallback every such target reads "no data" despite the metric existing one
    level up.
    """
    hit = latest_metrics(ctx, key).get(name)
    if hit is not None:
        return hit, key
    row = _row(ctx, key)
    if not row["parent_key"]:
        return None, None
    return latest_metrics(ctx, row["parent_key"]).get(name), row["parent_key"]


def evaluate_targets(ctx, key=None):
    """Compare each target against the latest observation of its metric."""
    out = []
    for t in list_targets(ctx, key):
        latest, source_key = _metric_with_parent_fallback(ctx, t["issue"], t["metric"])
        if latest is None:
            out.append({**t, "actual": None, "met": None, "gap": None,
                        "status": "no data"})
            continue
        t = {**t, "metric_from": source_key}
        met = OPS[t["op"]](latest["value"], t["value"])
        gap = t["value"] - latest["value"] if t["op"] in (">=", ">") \
            else latest["value"] - t["value"]
        out.append({
            **t, "actual": latest["value"], "step": latest["step"],
            "at": latest["at"], "met": met,
            "gap": 0.0 if met else round(gap, 6),
            "status": "met" if met else "not met",
        })
    return out


# -------------------------------------------------------------------- params


def _encode(value):
    """Store a param preserving its type, without a column per kind."""
    if isinstance(value, bool):
        return "true" if value else "false", "bool"
    if isinstance(value, int):
        return str(value), "int"
    if isinstance(value, float):
        return repr(value), "float"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value), "json"
    return str(value), "str"


def _decode(text, kind):
    try:
        if kind == "bool":
            return text == "true"
        if kind == "int":
            return int(text)
        if kind == "float":
            return float(text)
        if kind == "json":
            return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    return text


def set_params(ctx, key, params, source=None, actor=None):
    """Record run configuration. Re-setting a name overwrites it."""
    if not isinstance(params, dict) or not params:
        raise ValidationError("params must be a non-empty object", field="params")
    now = utcnow()
    with tx(ctx.conn):
        row = _row(ctx, key)
        for name, raw in params.items():
            name = str(name).strip()
            if not name:
                raise ValidationError("param names must not be empty", field="params")
            value, kind = _encode(raw)
            ctx.conn.execute(
                "INSERT INTO param(issue_id, name, value, type, source, at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(issue_id, name) DO UPDATE SET"
                " value=excluded.value, type=excluded.type,"
                " source=excluded.source, at=excluded.at",
                (row["id"], name, value, kind, source, now),
            )
        events.record(conn=ctx.conn, issue_id=row["id"], issue_key=row["key"],
                      actor=ctx.actor(actor), kind="updated", field="params",
                      new=",".join(sorted(params)), at=now)
    return list_params(ctx, key)


def list_params(ctx, key):
    row = _row(ctx, key)
    rows = ctx.conn.execute(
        "SELECT name, value, type, source, at FROM param WHERE issue_id=?"
        " ORDER BY name", (row["id"],)
    ).fetchall()
    return {r["name"]: _decode(r["value"], r["type"]) for r in rows}


def remove_param(ctx, key, name):
    with tx(ctx.conn):
        row = _row(ctx, key)
        cur = ctx.conn.execute("DELETE FROM param WHERE issue_id=? AND name=?",
                               (row["id"], name))
        if not cur.rowcount:
            raise NotFoundError(f"no param {name} on {row['key']}", name=name)
    return {"issue": row["key"], "name": name, "removed": True}


# ----------------------------------------------------------------- heartbeat


def heartbeat(ctx, key, ref="run", metrics=None, step=None, params=None,
              final_state=None, note=None, source=None, timeout_seconds=None):
    """Report liveness from a process that TAM cannot observe directly.

    A pulled watch reports `stopped` when its process dies. A pushing job that
    dies simply stops pushing, which is indistinguishable from working quietly
    -- so its watch records the time of the last beat and the heartbeat provider
    turns silence into a state.
    """
    from ..providers import STATES

    if final_state is not None and final_state not in STATES:
        raise ValidationError(
            f"final_state must be one of {', '.join(STATES)}", field="final_state")

    now = utcnow()
    with tx(ctx.conn):
        row = _row(ctx, key)
        existing = ctx.conn.execute(
            "SELECT id, config, detail FROM watch"
            " WHERE issue_id=? AND provider='heartbeat' AND ref=?",
            (row["id"], ref),
        ).fetchone()

        detail = json.loads(existing["detail"]) if existing and existing["detail"] else {}
        detail["last_beat"] = now
        detail["beats"] = int(detail.get("beats", 0)) + 1
        if note:
            detail["note"] = note
        if final_state:
            detail["final_state"] = final_state

        if existing:
            config = json.loads(existing["config"]) if existing["config"] else {}
            if timeout_seconds:
                config["timeout_seconds"] = int(timeout_seconds)
            ctx.conn.execute(
                "UPDATE watch SET detail=?, config=?, last_seen=? WHERE id=?",
                (json.dumps(detail), json.dumps(config) if config else None,
                 now, existing["id"]),
            )
        else:
            config = {"timeout_seconds": int(timeout_seconds)} if timeout_seconds else {}
            ctx.conn.execute(
                "INSERT INTO watch(issue_id, provider, ref, label, config, detail,"
                " last_seen, created_at) VALUES (?,'heartbeat',?,?,?,?,?,?)",
                (row["id"], ref, source, json.dumps(config) if config else None,
                 json.dumps(detail), now, now),
            )

    if params:
        set_params(ctx, key, params, source=source or ref)
    written = {}
    for name, value in (metrics or {}).items():
        record_metric(ctx, key, name, value, step=step, source=source or ref)
        written[name] = value
    return {"issue": row["key"], "ref": ref, "at": now,
            "beats": detail["beats"], "metrics": written,
            "final_state": final_state}
