#!/usr/bin/env python3
"""Snapshot the TAM board for offline analysis.

Writes two files into the working directory:

    events.json        every event since --since (change feed, newest first)
    issues_full.json   every issue in every status, with its body

    TAM_API_URL=... TAM_API_TOKEN=... python3 fetch_tam.py WORKDIR [--since 2026-01-01T00:00:00Z]

Read-only: it only issues GET requests. The token is read from the environment
and never written to disk.
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

STATUSES = "backlog,todo,in_progress,blocked,review,done,cancelled"


def get(path: str):
    url = os.environ["TAM_API_URL"].rstrip("/") + path
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['TAM_API_TOKEN']}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("workdir")
    ap.add_argument("--since", default="2026-01-01T00:00:00Z")
    args = ap.parse_args()
    for var in ("TAM_API_URL", "TAM_API_TOKEN"):
        if not os.environ.get(var):
            print(f"{var} is not set (see docs/ARGUS.md, Setup)", file=sys.stderr)
            return 2
    out = Path(args.workdir)
    out.mkdir(parents=True, exist_ok=True)
    events = get(f"/api/events?since={args.since}&limit=20000")
    issues = get(f"/api/issues?status={STATUSES}&limit=2000&brief=false")
    (out / "events.json").write_text(json.dumps(events))
    (out / "issues_full.json").write_text(json.dumps(issues))
    n_ev = len(events.get("data", []))
    data = issues.get("data", issues)
    n_is = len(data.get("issues", data)) if isinstance(data, dict) else len(data)
    print(f"events {n_ev}, issues {n_is} -> {out}")
    if n_ev >= 20000:
        print("warning: event limit reached, raise it or narrow --since", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
