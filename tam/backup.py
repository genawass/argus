"""Database backups.

The entire task history is one SQLite file on one host's local disk. That is
the deliberate trade for safe writes -- but it means this file is the only
copy, and the disk under it is the only redundancy. A backup that lands on the
same disk protects against a bad migration and nothing else, so `backup_dir`
should name another filesystem (README: Backups and recovery).

`sqlite3.Connection.backup` is used rather than copying the file, because a
plain copy of a live WAL database can capture a torn state.
"""

import sqlite3
from pathlib import Path

from .clock import utcnow


def default_dir(config):
    return config.backup_path


def run(config, dest=None, keep=14):
    """Write a consistent snapshot and prune old ones. Returns what it did."""
    dest = Path(dest) if dest else default_dir(config)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().replace(":", "").replace("-", "")
    out = dest / f"tam-{stamp}.db"

    source = sqlite3.connect(str(config.db_path))
    try:
        target = sqlite3.connect(str(out))
        try:
            source.backup(target)          # consistent even against live writers
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
    finally:
        source.close()

    integrity = "unknown"
    check = sqlite3.connect(str(out))
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        issues = check.execute("SELECT COUNT(*) FROM issue").fetchone()[0]
    finally:
        check.close()
    if integrity != "ok":
        out.unlink(missing_ok=True)
        from .errors import TamError
        raise TamError(f"backup failed its integrity check: {integrity}")

    existing = sorted(dest.glob("tam-*.db"))
    pruned = []
    if keep and len(existing) > keep:
        for old in existing[:len(existing) - keep]:
            old.unlink(missing_ok=True)
            pruned.append(old.name)

    return {
        "path": str(out),
        "bytes": out.stat().st_size,
        "issues": issues,
        "integrity": integrity,
        "kept": min(len(existing), keep) if keep else len(existing),
        "pruned": pruned,
    }
