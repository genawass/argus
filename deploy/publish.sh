#!/bin/sh
# Publish this working tree to the shared copy every node runs from.
#
# The API and timers on the database owner execute from this repository on
# local disk; every other node picks up /mnt/datasets/tam/code via the PATH
# that env.sh sets. Those two used to be kept in step by hand, which is how
# they silently diverged. This script is the only supported way to move code
# from here to there.
#
# It refuses to publish a dirty tree on purpose: whatever is running on the
# cluster should be a commit you can name, not somebody's half-finished edit.
#
# Usage:
#   deploy/publish.sh            publish HEAD
#   deploy/publish.sh -n         dry run, show what would change
#   deploy/publish.sh -f         publish anyway with uncommitted changes
set -eu

DEST=${TAM_CODE_DEST:-/mnt/datasets/tam/code}
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
DRY=""
FORCE=0

while [ $# -gt 0 ]; do
    case $1 in
        -n|--dry-run) DRY="--dry-run" ;;
        -f|--force)   FORCE=1 ;;
        *) echo "usage: $(basename "$0") [-n|--dry-run] [-f|--force]" >&2; exit 2 ;;
    esac
    shift
done

cd "$ROOT"

if [ ! -d .git ]; then
    echo "ERROR: $ROOT is not a git repository." >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    if [ "$FORCE" -eq 0 ]; then
        echo "ERROR: working tree is dirty. Commit first, or pass --force." >&2
        git status --short >&2
        exit 1
    fi
    echo "WARNING: publishing a dirty tree (--force)." >&2
fi

if [ ! -d "$DEST" ]; then
    echo "ERROR: destination not found: $DEST" >&2
    echo "Is /mnt/datasets mounted?" >&2
    exit 1
fi

REV=$(git rev-parse --short HEAD)
echo "Publishing $REV -> $DEST"

# --delete so a file removed here is removed there; without it the shared copy
# accumulates code nobody can account for. The excludes are the things that are
# per-host and must never be published: the local database, caches, history.
#
# rsync writes each file to a temporary name and renames it into place, so a
# node importing a module during a publish sees either the old file or the new
# one -- never a half-written one. Do not replace this with cp.
# A dry run whose whole point is showing the diff needs --itemize-changes;
# without it rsync prints nothing and "no output" reads as "no changes".
[ -n "$DRY" ] && DRY="$DRY --itemize-changes"

rsync -a --delete $DRY \
    --exclude '.git/' \
    --exclude 'data/' \
    --exclude 'data.local-backup-*/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    "$ROOT"/ "$DEST"/

if [ -z "$DRY" ]; then
    # Leave a note of what is out there, so a node can be asked what it is
    # running without guessing from timestamps.
    printf '%s\n' "$(git rev-parse HEAD)" > "$DEST/.published-from"
    echo "Published $REV"
else
    echo "(dry run -- nothing changed)"
fi
