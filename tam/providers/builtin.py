"""Providers shipped with TAM.

`slurm`, `process` and `path` are conveniences for common cases. `command` and
`http` are the general ones: between them they cover any tool with a CLI or a
REST API -- Encord, GCP, Vertex AI, a CI server -- with configuration rather
than code.
"""

import json
import os
import re
import shutil
import urllib.error
import urllib.request

from .. import parsers
from ..shell import run, sh, ssh
from . import (FAILED, MISSING, PENDING, PRESENT, RUNNING, STOPPED, SUCCEEDED,
               UNKNOWN, UNREACHABLE, Observation, Provider, register)


def _read_text(path, host=None, cap=400_000, lines=4000):
    """Tail a file, locally or over ssh, bounded in both bytes and lines."""
    script = f"tail -c {cap} {json.dumps(path)} 2>/dev/null | tail -n {lines}"
    rc, out, _ = ssh(host, script) if host else sh(script)
    return out if (rc == 0 or out) else ""


def _extract(watch, text):
    cfg = Provider.config_of(watch)
    if not text or not cfg.get("parser"):
        return {}, None
    return parsers.extract(text, cfg)


# --------------------------------------------------------------------- slurm

SLURM_STATES = {
    "RUNNING": RUNNING, "COMPLETING": RUNNING, "CONFIGURING": RUNNING,
    "PENDING": PENDING, "REQUEUED": PENDING, "RESIZING": PENDING,
    "SUSPENDED": PENDING,
    "COMPLETED": SUCCEEDED,
    "FAILED": FAILED, "TIMEOUT": FAILED, "OUT_OF_MEMORY": FAILED,
    "NODE_FAIL": FAILED, "BOOT_FAIL": FAILED, "DEADLINE": FAILED,
    "CANCELLED": STOPPED, "PREEMPTED": STOPPED, "REVOKED": STOPPED,
}


class SlurmProvider(Provider):
    name = "slurm"
    description = "Slurm job by id (squeue while queued, sacct after)"
    ref_hint = "job id, e.g. 38574"

    def probe(self, watch):
        ref = watch["ref"]
        if not shutil.which("squeue"):
            return Observation(UNKNOWN, detail={"note": "squeue not on this host"})

        rc, out, _ = run(["squeue", "-j", str(ref), "-h",
                          "-o", "%T|%M|%N|%R|%Z|%j"])
        line = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
        if line:
            native, elapsed, node, reason, workdir, name = (
                line.split("|") + [""] * 6)[:6]
            detail = {"elapsed": elapsed.strip(), "node": node.strip(),
                      "reason": reason.strip(), "workdir": workdir.strip(),
                      "name": name.strip(), "live": True}
            return self._with_metrics(watch, native.strip(), detail)

        if shutil.which("sacct"):
            rc, out, _ = run(["sacct", "-j", str(ref), "-n", "-P", "-X",
                              "-o", "State,Elapsed,ExitCode,End,JobName"])
            row = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
            if row:
                native, elapsed, code, end, name = (row.split("|") + [""] * 5)[:5]
                detail = {"elapsed": elapsed.strip(), "exit_code": code.strip(),
                          "ended": end.strip(), "name": name.strip(), "live": False}
                return self._with_metrics(watch, native.strip().split()[0], detail)

        return Observation(MISSING, detail={"note": "not in squeue or sacct"})

    def _with_metrics(self, watch, native, detail):
        state = SLURM_STATES.get(native.upper(), UNKNOWN)
        cfg = self.config_of(watch)
        # `scontrol` forgets a job as soon as it leaves the queue, so the log
        # path must be remembered from an earlier probe. Without this the abort
        # check and the final metrics are unavailable precisely when the job
        # ends -- which is when they matter most.
        log = (cfg.get("log")
               or self._stdout_path(watch["ref"])
               or (watch.get("detail") or {}).get("log"))
        metrics, step = ({}, None)
        text = ""
        if log and (cfg.get("parser") or cfg.get("state_patterns")):
            text = _read_text(log, cfg.get("log_host"))
            detail["log"] = log
        if text and cfg.get("parser"):
            metrics, step = _extract(watch, text)

        # The scheduler only knows whether the batch script exited 0. A wrapper
        # that traps a kill, cleans up and exits cleanly is reported COMPLETED
        # even though the work inside was aborted. `state_patterns` lets a watch
        # say what the log itself must not contain -- same key and meaning as
        # the command provider.
        if text:
            for override, pattern in (cfg.get("state_patterns") or {}).items():
                if re.search(pattern, text, re.MULTILINE):
                    detail["state_override"] = {
                        "matched": pattern, "scheduler_said": native}
                    state = override
                    break
        return Observation(state, native_state=native, detail=detail,
                           metrics=metrics, step=step)

    @staticmethod
    def _stdout_path(job_id):
        rc, out, _ = run(["scontrol", "show", "job", str(job_id)])
        if rc != 0:
            return None
        m = re.search(r"StdOut=(\S+)", out)
        return m.group(1) if m else None


# ------------------------------------------------------------------- process


class ProcessProvider(Provider):
    name = "process"
    description = "A process matching a pattern, locally or on a host"
    ref_hint = "substring of the command line"

    def probe(self, watch):
        script = (f"ps -eo pid,etime,pcpu,rss,args | grep -v grep | "
                  f"grep -F -- {json.dumps(watch['ref'])} | head -3")
        rc, out, err = ssh(watch.get("host"), script)
        if "Permission denied" in (err or "") or "Could not resolve" in (err or ""):
            return Observation(UNREACHABLE,
                               detail={"note": f"cannot reach {watch.get('host')}"})
        lines = [l for l in (out or "").strip().splitlines() if l.strip()]
        if not lines:
            return Observation(STOPPED, native_state="stopped", detail={"matches": 0})
        parts = lines[0].split(None, 4)
        return Observation(RUNNING, native_state="running", detail={
            "matches": len(lines), "pid": parts[0] if parts else None,
            "elapsed": parts[1] if len(parts) > 1 else None,
            "cpu": parts[2] if len(parts) > 2 else None})


# ---------------------------------------------------------------------- path


class PathProvider(Provider):
    name = "path"
    description = "A file or directory: does it exist, and is it still growing?"
    ref_hint = "filesystem path"

    def probe(self, watch):
        ref = watch["ref"]
        script = (f"p={json.dumps(ref)}; if [ -e \"$p\" ]; then "
                  f"n=$(find \"$p\" -type f 2>/dev/null | wc -l); "
                  f"m=$(find \"$p\" -type f -printf '%T@\\n' 2>/dev/null "
                  f"| sort -rn | head -1); echo \"$n|$m\"; "
                  f"else echo MISSING; fi")
        rc, out, err = ssh(watch.get("host"), script)
        text = (out or "").strip()
        if "Permission denied" in (err or ""):
            return Observation(UNREACHABLE,
                               detail={"note": f"cannot reach {watch.get('host')}"})
        if not text or text == "MISSING":
            return Observation(MISSING)
        count, _, mtime = text.partition("|")
        try:
            count = int(count)
        except ValueError:
            return Observation(UNKNOWN, detail={"raw": text})
        detail = {"files": count,
                  "newest_epoch": float(mtime) if mtime.strip() else None}

        cfg = self.config_of(watch)
        metrics, step = ({}, None)
        if cfg.get("parser") and cfg.get("file"):
            metrics, step = _extract(watch, _read_text(cfg["file"], watch.get("host")))
        if cfg.get("count_metric"):
            metrics[cfg["count_metric"]] = float(count)
        return Observation(PRESENT, native_state="present", detail=detail,
                           metrics=metrics, step=step)


# ------------------------------------------------------------------- command


class CommandProvider(Provider):
    name = "command"
    description = ("Run any command; map its output to a state and metrics. "
                   "Use for tools without a dedicated provider (gcloud, CLIs)")
    ref_hint = "shell command to run"

    def probe(self, watch):
        cfg = self.config_of(watch)
        rc, out, err = ssh(watch.get("host"), watch["ref"]) if watch.get("host") \
            else sh(watch["ref"])
        text = out or ""
        detail = {"exit_code": rc}
        if cfg.get("include_output"):
            detail["output"] = text[-2000:]

        state = self._state_from(cfg, rc, text)
        metrics, step = _extract(watch, text)
        return Observation(state, native_state=str(rc), detail=detail,
                           metrics=metrics, step=step)

    @staticmethod
    def _state_from(cfg, rc, text):
        # An explicit mapping wins; otherwise the exit code decides.
        for state, pattern in (cfg.get("state_patterns") or {}).items():
            if re.search(pattern, text, re.MULTILINE):
                return state
        if cfg.get("state_from") == "output":
            return (text.strip().splitlines() or [UNKNOWN])[-1].strip().lower()
        return SUCCEEDED if rc == 0 else FAILED


# ---------------------------------------------------------------------- http


class HttpProvider(Provider):
    name = "http"
    description = ("GET a URL and read state/metrics out of the response. "
                   "Use for REST APIs (Encord, GCP, CI) without writing code")
    ref_hint = "URL"

    def probe(self, watch):
        cfg = self.config_of(watch)
        headers = dict(cfg.get("headers") or {})
        # Never store a credential in the database: name an env var instead.
        if cfg.get("token_env") and os.environ.get(cfg["token_env"]):
            scheme = cfg.get("token_scheme", "Bearer")
            headers["Authorization"] = f"{scheme} {os.environ[cfg['token_env']]}"

        req = urllib.request.Request(watch["ref"], headers=headers,
                                     method=cfg.get("method", "GET"))
        try:
            with urllib.request.urlopen(req, timeout=cfg.get("timeout", 20)) as resp:
                text = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            return Observation(FAILED, native_state=str(exc.code),
                               detail={"http_status": exc.code})
        except (urllib.error.URLError, OSError) as exc:
            return Observation(UNREACHABLE, detail={"note": str(exc)})

        detail = {"http_status": status}
        native = None
        state = PRESENT
        if cfg.get("state_path"):
            native = str(parsers._dig(_safe_json(text), cfg["state_path"]))
            mapping = {k.lower(): v for k, v in (cfg.get("state_map") or {}).items()}
            state = mapping.get(native.lower(), UNKNOWN)
            detail["reported_state"] = native
        metrics, step = _extract(watch, text)
        return Observation(state, native_state=native, detail=detail,
                           metrics=metrics, step=step)


class HeartbeatProvider(Provider):
    """Liveness for jobs TAM cannot see: silence becomes a state.

    The job reports in via `tam heartbeat` (or the reporter client). If nothing
    has arrived within the timeout, the watch goes `stopped` -- which is the
    whole point: a pushing job that dies otherwise looks exactly like one that
    is working quietly.
    """

    name = "heartbeat"
    description = "A job that reports in; goes stopped when it falls silent"
    ref_hint = "a name for the reporter, e.g. train-loop"

    DEFAULT_TIMEOUT = 900          # 15 minutes, matching the scan interval

    def probe(self, watch):
        from ..clock import parse_instant, utcnow

        cfg = self.config_of(watch)
        detail = dict(watch.get("detail") or {})
        last = detail.get("last_beat")

        # A job that reported its own ending is not "silent" -- it is finished.
        final = detail.get("final_state")
        if final:
            return Observation(final, native_state="reported", detail=detail)

        if not last:
            return Observation(PENDING, native_state="never reported",
                               detail=detail)

        timeout = int(cfg.get("timeout_seconds") or self.DEFAULT_TIMEOUT)
        try:
            age = (parse_instant(utcnow()) - parse_instant(last)).total_seconds()
        except (ValueError, TypeError):
            return Observation(UNKNOWN, detail=detail)

        detail["age_seconds"] = int(age)
        detail["timeout_seconds"] = timeout
        if age <= timeout:
            return Observation(RUNNING, native_state="beating", detail=detail)
        return Observation(STOPPED, native_state="silent", detail=detail)


def _safe_json(text):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}


for provider in (SlurmProvider, ProcessProvider, PathProvider,
                 CommandProvider, HttpProvider, HeartbeatProvider):
    register(provider)
