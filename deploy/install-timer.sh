#!/bin/sh
# Install the systemd --user timer that builds the daily digest at 07:55.
#
# Idempotent: re-running re-renders the units and restarts the timer.
set -eu

ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# Same resolution the entrypoints use, so units and shells agree on which
# database is "the" database. Only matters when TAM_HOME is not already set.
if [ -r "${TAM_ENV:-$ROOT/.tam-env}" ]; then
    . "${TAM_ENV:-$ROOT/.tam-env}"
fi

mkdir -p "$UNIT_DIR"
for unit in tam-api.service \
            tam-digest.service tam-digest.timer \
            tam-scan.service tam-scan.timer \
            tam-backup.service tam-backup.timer; do
    sed -e "s|__TAM_ROOT__|$ROOT|g" -e "s|__TAM_HOME__|${TAM_HOME:-$ROOT}|g" "$ROOT/deploy/$unit" > "$UNIT_DIR/$unit"
    echo "wrote $UNIT_DIR/$unit"
done

systemctl --user daemon-reload
# The API first: the timers write through the same database file, but a node
# that wakes up mid-install should find the service, not a connection refused.
systemctl --user enable --now tam-api.service
systemctl --user restart tam-api.service
for t in tam-digest.timer tam-scan.timer tam-backup.timer; do
    systemctl --user enable --now "$t"
done

echo
systemctl --user list-timers 'tam-*.timer' --no-pager || true

# User timers only fire while a user manager is running. Without lingering
# that means "while you are logged in" -- which is usually not what you want
# for an 08:00 job on a laptop that boots into a display manager.
if ! loginctl show-user "$(id -un)" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    echo
    echo "note: lingering is off, so the timer only runs while you are logged in."
    echo "      enable it with:  sudo loginctl enable-linger $(id -un)"
fi

echo
"$ROOT/deploy/install-agent-instructions.sh"
