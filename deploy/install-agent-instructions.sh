#!/bin/sh
# Install persistent, merge-safe TAM guidance for Codex, Claude, and Gemini.
set -eu

ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
exec python3 "$ROOT/tools/install_agent_instructions.py" --root "$ROOT" "$@"
