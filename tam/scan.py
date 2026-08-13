"""Scanner: ask each watch's provider what it sees, and record the answer.

This module contains no knowledge of any particular tool. It resolves the
provider named on the watch, asks it to probe, and stores the result in the
canonical vocabulary. Slurm, Encord, GCP and a bare shell command all arrive
here identically.
"""

import json

from . import providers
from .clock import utcnow
from .errors import TamError


def scan(svc, key=None, record_metrics=True):
    """Probe every watch, update state, record metrics, comment on changes."""
    from .core import observe

    providers.load_user_providers(svc.config.data_dir / "providers")

    results = []
    for w in observe.list_watches(svc, key=key):
        try:
            provider = providers.get(w["provider"])
            obs = provider.probe(w)
        except TamError as exc:
            obs = providers.Observation(providers.UNKNOWN,
                                        detail={"error": exc.message})
        except Exception as exc:  # noqa: BLE001 - one bad provider must not
            obs = providers.Observation(  # stop the rest of the scan
                providers.UNKNOWN, detail={"error": repr(exc)})

        recorded = {}
        if record_metrics and obs.metrics:
            recorded = _record(svc, observe, w, obs)

        previous = observe.update_watch_state(
            svc, w["id"], obs.state, obs.native_state, obs.detail)
        changed = previous != obs.state
        # First observation is not a change worth narrating -- there is no
        # previous state to have moved from.
        commented = changed and previous is not None
        if commented:
            svc.add_comment(
                w["issue"],
                f"[scan] {w['provider']}:{w['ref']} changed {previous} -> {obs.state}"
                + (f" ({obs.native_state})" if obs.native_state else "")
                + (f"\n{json.dumps(obs.detail, indent=2)}" if obs.detail else ""),
                author="scanner",
            )

        results.append({**w, "state": obs.state, "native_state": obs.native_state,
                        "detail": obs.detail, "previous": previous,
                        "changed": changed, "commented": commented,
                        "metrics": recorded})

    return {"scanned_at": utcnow(), "watches": len(results), "results": results,
            "changed": [r for r in results if r["changed"]],
            "commented": [r for r in results if r["commented"]],
            "attention": [r for r in results if r["state"] in providers.ATTENTION]}


def _record(svc, observe, watch, obs):
    """Store metrics, skipping points identical to the last one.

    Without this, a timer polling an idle source would grow the series forever.
    """
    latest = observe.latest_metrics(svc, watch["issue"])
    written = {}
    for name, value in obs.metrics.items():
        previous = latest.get(name)
        if previous and previous["value"] == value and previous["step"] == obs.step:
            continue
        observe.record_metric(svc, watch["issue"], name, value, step=obs.step,
                              source=f"{watch['provider']}:{watch['ref']}")
        written[name] = value
    return written
