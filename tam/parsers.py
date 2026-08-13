"""Metric extractors, selected per watch.

Probing ("is it running?") and measuring ("what is recall now?") are separate
concerns. Keeping extraction here means a provider does not need to know any
log format, and a new format is a parser rather than a change to a provider.

Choose one per watch via config, e.g.
    {"parser": "yolo", "log": "/nfs/slurm_logs/job.out"}
    {"parser": "regex", "patterns": {"loss": "loss=([0-9.]+)"}}
    {"parser": "json", "paths": {"recall": "metrics.recall"}}
"""

import json
import re

from .errors import ValidationError

_PARSERS = {}


def parser(name):
    def wrap(fn):
        _PARSERS[name] = fn
        return fn
    return wrap


def available():
    return sorted(_PARSERS)


def extract(text, config):
    """Run the configured parser over `text`. Returns (metrics, step)."""
    name = (config or {}).get("parser")
    if not name:
        return {}, None
    if name not in _PARSERS:
        raise ValidationError(
            f"unknown parser {name!r}; available: {', '.join(available())}",
            field="parser")
    return _PARSERS[name](text, config or {})


# YOLO-family validation summary:
#   "all <images> <labels> <P> <R> <mAP50> <mAP50-95>"
YOLO_ROW = re.compile(
    r"^\s+all\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$")
# Progress lines carry "<step>/<total>".
STEP_ROW = re.compile(r"^\s*(\d+)/(\d+)\s")


@parser("yolo")
def parse_yolo(text, config):
    rows = [m.groups() for line in text.splitlines() if (m := YOLO_ROW.match(line))]
    if not rows:
        return {}, None
    _images, _labels, p, r, m50, m5095 = rows[-1]
    steps = [m.group(1) for line in text.splitlines() if (m := STEP_ROW.match(line))]
    return ({"precision": float(p), "recall": float(r),
             "map50": float(m50), "map50_95": float(m5095)},
            int(steps[-1]) if steps else None)


@parser("regex")
def parse_regex(text, config):
    """config: {"patterns": {"metric_name": "regex with one capture group"}}"""
    metrics = {}
    for name, pattern in (config.get("patterns") or {}).items():
        found = re.findall(pattern, text, re.MULTILINE)
        if found:
            last = found[-1]
            last = last[0] if isinstance(last, tuple) else last
            try:
                metrics[name] = float(last)
            except (TypeError, ValueError):
                continue
    step = None
    if config.get("step_pattern"):
        found = re.findall(config["step_pattern"], text, re.MULTILINE)
        if found:
            last = found[-1]
            last = last[0] if isinstance(last, tuple) else last
            try:
                step = int(float(last))
            except (TypeError, ValueError):
                step = None
    return metrics, step


def _dig(obj, path):
    for part in path.split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


@parser("json")
def parse_json(text, config):
    """config: {"paths": {"recall": "metrics.recall"}, "step_path": "epoch"}"""
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}, None
    metrics = {}
    for name, path in (config.get("paths") or {}).items():
        value = _dig(doc, path)
        try:
            metrics[name] = float(value)
        except (TypeError, ValueError):
            continue
    step = None
    if config.get("step_path"):
        try:
            step = int(float(_dig(doc, config["step_path"])))
        except (TypeError, ValueError):
            step = None
    return metrics, step
