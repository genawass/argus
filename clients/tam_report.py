"""Minimal TAM reporter for training and processing scripts.

Copy this file next to your code. Standard library only, no install.

    from tam_report import Reporter

    run = Reporter("TAM-14", ref="example-finetune")
    run.params(lr=0.001, batch=24, dataset="example_dataset")
    for epoch in range(epochs):
        ...
        run.metric(step=epoch, recall=r, precision=p, map50=m)
    run.done()

Configuration comes from the environment, so nothing is hard-coded:

    TAM_API_URL     required, e.g. http://10.0.0.10:8787
    TAM_API_TOKEN   or TAM_API_TOKEN_FILE

Design rule: **reporting must never break the job it reports on.** Every call
swallows its errors and warns on stderr. A TAM outage costs you telemetry, not
a training run.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

__all__ = ["Reporter"]


def _token():
    token = os.environ.get("TAM_API_TOKEN")
    if token:
        return token.strip()
    path = os.environ.get("TAM_API_TOKEN_FILE")
    if not path and os.environ.get("TAM_HOME"):
        path = os.path.join(os.environ["TAM_HOME"], "data", "api_token")
    if path and os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    return None


class Reporter:
    """Reports run configuration, metrics and liveness to a TAM issue."""

    def __init__(self, issue, ref="run", url=None, token=None, timeout=10,
                 heartbeat_seconds=None, quiet=False, timeout_seconds=900):
        self.issue = issue
        self.ref = ref
        self.url = (url or os.environ.get("TAM_API_URL") or "").rstrip("/")
        self.token = token or _token()
        self.timeout = timeout
        self.quiet = quiet
        self.timeout_seconds = timeout_seconds
        self.enabled = bool(self.url)
        self.failures = 0
        self._stop = None

        if not self.enabled and not quiet:
            self._warn("TAM_API_URL is not set; reporting disabled")
        elif heartbeat_seconds:
            self._start_background(heartbeat_seconds)

    # -- public ----------------------------------------------------------
    def params(self, _mapping=None, **kw):
        """Record run configuration. Call once, early."""
        payload = dict(_mapping or {})
        payload.update(kw)
        return self._post(f"/api/issues/{self.issue}/params",
                          {"params": payload, "source": self.ref})

    def metric(self, step=None, **metrics):
        """Record metrics and beat at the same time."""
        return self._beat(metrics=metrics, step=step)

    def beat(self, note=None):
        """Liveness only, when there is nothing new to measure."""
        return self._beat(note=note)

    def done(self, note=None):
        self._stop_background()
        return self._beat(final_state="succeeded", note=note)

    def failed(self, note=None):
        self._stop_background()
        return self._beat(final_state="failed", note=note)

    # -- context manager -------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # An exception in the job is reported as a failed run, not swallowed.
        if exc_type is None:
            self.done()
        else:
            self.failed(note=f"{exc_type.__name__}: {exc}")
        return False

    # -- internals -------------------------------------------------------
    def _beat(self, metrics=None, step=None, final_state=None, note=None):
        body = {"ref": self.ref, "source": self.ref,
                "timeout_seconds": self.timeout_seconds}
        if metrics:
            body["metrics"] = {k: float(v) for k, v in metrics.items()
                               if v is not None}
        if step is not None:
            body["step"] = int(step)
        if final_state:
            body["final_state"] = final_state
        if note:
            body["note"] = str(note)[:500]
        return self._post(f"/api/issues/{self.issue}/heartbeat", body)

    def _post(self, path, body):
        if not self.enabled:
            return None
        req = urllib.request.Request(
            self.url + path, method="POST",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + self.token}
                        if self.token else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self.failures = 0
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            self._warn(f"HTTP {exc.code} from TAM: {exc.read()[:200]!r}")
        except Exception as exc:                      # noqa: BLE001
            self._warn(f"could not reach TAM: {exc}")
        self.failures += 1
        return None

    def _start_background(self, seconds):
        self._stop = threading.Event()

        def loop():
            while not self._stop.wait(seconds):
                self._beat()

        thread = threading.Thread(target=loop, name="tam-heartbeat", daemon=True)
        thread.start()

    def _stop_background(self):
        if self._stop is not None:
            self._stop.set()

    def _warn(self, message):
        if not self.quiet:
            sys.stderr.write(f"[tam_report] {message}\n")
