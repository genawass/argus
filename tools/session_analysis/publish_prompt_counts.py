#!/usr/bin/env python3
"""Record human-prompt counts per issue on the board, as a metric.

The daily digest lists open issues whose `human_prompts` metric is at or above
STEERING_MIN (see tam/digest.py). TAM cannot compute it itself: the counts come
from session transcripts, which live on the machines the agents ran on.

    python3 publish_prompt_counts.py WORKDIR            # dry run: show what would be written
    python3 publish_prompt_counts.py WORKDIR --apply    # POST the metrics

Reads WORKDIR/issue_features.jsonl (written by attribute.py). Writes one metric
point per issue with a fixed step and source, so re-running updates the value in
place instead of piling up points. Only issues with attributable session
activity are written, and containers are skipped; an issue with no transcript
gets nothing rather than a misleading zero.

A count is the number of human messages inside the issue's working window in
sessions that referenced it. A session steering several issues at once counts
for each of them, so treat high values as "worth a look", not as a measurement.
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

METRIC = "human_prompts"      # keep in step with tam.digest.STEERING_METRIC
SOURCE = "session-analysis"


def post(path: str, body: dict):
    req = urllib.request.Request(
        os.environ["TAM_API_URL"].rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['TAM_API_TOKEN']}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("workdir")
    ap.add_argument("--apply", action="store_true", help="write to the board (default: dry run)")
    ap.add_argument("--min-prompts", type=int, default=0,
                    help="only publish issues with at least this many prompts")
    args = ap.parse_args()

    work = Path(args.workdir)
    rows = [json.loads(line) for line in (work / "issue_features.jsonl").open()]
    # Containers (issues other issues hang off) span every child's sessions, so
    # their count is not a steering signal; the digest ignores them too.
    parents = {json.loads(line)["parent"] for line in (work / "issue_table.jsonl").open()} - {None}
    rows = [r for r in rows
            if r["covered"] and r["prompts"] >= args.min_prompts and r["key"] not in parents]
    rows.sort(key=lambda r: -r["prompts"])
    print(f"{len(rows)} issue(s) with session activity" + ("" if args.apply else " (dry run)"))
    for r in rows[:15]:
        print(f"  {r['key']:<9} {r['prompts']:>3} prompts  {r['outcome']:<22} {r['title'][:60]}")
    if len(rows) > 15:
        print(f"  ... and {len(rows) - 15} more")
    if not args.apply:
        print("re-run with --apply to write these as the "
              f"'{METRIC}' metric (source '{SOURCE}')")
        return 0

    for var in ("TAM_API_URL", "TAM_API_TOKEN"):
        if not os.environ.get(var):
            print(f"{var} is not set (see docs/ARGUS.md, Setup)", file=sys.stderr)
            return 2
    failed = 0
    for r in rows:
        try:
            post(f"/api/issues/{r['key']}/metrics",
                 {"name": METRIC, "value": r["prompts"], "step": 0, "source": SOURCE})
        except Exception as exc:                                  # keep going; report at the end
            failed += 1
            print(f"  {r['key']}: {exc}", file=sys.stderr)
    print(f"wrote {len(rows) - failed}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
