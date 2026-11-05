#!/usr/bin/env bash
# Run the deterministic stages of the session analysis (everything except the
# model classification, which needs subagents; see README.md).
#
#   EXCLUDE_SESSION=<id prefix of the analysing session> \
#   TAM_API_URL=... TAM_API_TOKEN=... \
#   tools/session_analysis/run_all.sh WORKDIR
#
# WORKDIR holds derived data (counts, issue keys, redacted digests). Keep it
# outside the repo: digests contain issue and comment text.
set -euo pipefail

WORK=${1:?usage: run_all.sh WORKDIR}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$WORK"

run() { echo "== $*"; python3 -I "$HERE/$1" "${@:2}"; }

run fetch_tam.py "$WORK"
run extract_sessions.py "$WORK"
run extract_codex.py "$WORK"
run timelines.py "$WORK"
run outcomes.py "$WORK"
run attribute.py "$WORK"
run build_digests.py "$WORK"

cat <<EOF

Digests are in $WORK/digests, batch lists in $WORK/batches.json.
Next: classify each batch with the prompts in $HERE/prompts/ (one subagent per
batch, writing $WORK/labels/batch_N.json), then:

  python3 -I $HERE/aggregate.py $WORK
  python3 -I $HERE/hyp.py $WORK
EOF
