"""SQLite connection, pragmas, and the migration runner.

WAL plus a busy timeout is what lets the CLI, the HTTP server and the MCP
server share one database file safely. Connections run in autocommit mode
(`isolation_level=None`) so transactions are explicit and `executescript`
behaves predictably; writes go through `tx()`, which takes an IMMEDIATE lock
up front rather than discovering the contention halfway through.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Filesystems where SQLite cannot be safely opened from more than one host.
# WAL needs a shared-memory file that does not work across machines, and
# rollback-journal mode still drops writes when two hosts contend. Measured on
# an NFSv4.2 mount: WAL lost 100% of the second host's writes, TRUNCATE 11%.
NETWORK_FS = {"nfs", "nfs4", "cifs", "smbfs", "smb3", "fuse.sshfs", "afs", "lustre",
              "glusterfs", "ceph", "9p"}


def filesystem_type(path):
    """Filesystem type of the mount containing `path`, or '' if undetermined."""
    try:
        target = Path(path).resolve()
        mounts = []
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                mounts.append((parts[1], parts[2]))
        best, best_type = "", ""
        for mount_point, fstype in mounts:
            if (str(target) == mount_point
                    or str(target).startswith(mount_point.rstrip("/") + "/")):
                if len(mount_point) >= len(best):
                    best, best_type = mount_point, fstype
        return best_type
    except OSError:
        return ""


def is_network_fs(path):
    return filesystem_type(path) in NETWORK_FS

FTS_SQL = """
CREATE VIRTUAL TABLE issue_fts USING fts5(
    title, body, content='issue', content_rowid='id');

CREATE TRIGGER issue_fts_ai AFTER INSERT ON issue BEGIN
  INSERT INTO issue_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER issue_fts_ad AFTER DELETE ON issue BEGIN
  INSERT INTO issue_fts(issue_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER issue_fts_au AFTER UPDATE ON issue BEGIN
  INSERT INTO issue_fts(issue_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO issue_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
"""


def connect(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate(conn)
    return conn


@contextmanager
def tx(conn):
    """Explicit write transaction. Nested use is a no-op guard, not a savepoint."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _run_script(conn, body, tail=""):
    """Run a multi-statement script atomically.

    `executescript` issues an implicit COMMIT before it starts, so an outer
    BEGIN would be discarded. The transaction therefore has to live inside the
    script text itself.
    """
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + body + "\n" + tail + "\nCOMMIT;")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate(conn):
    """Apply pending migrations. Forward-only, one transaction per file."""
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    current = int(_meta_get(conn, "schema_version", "0"))

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version <= current:
            continue
        # The version bump rides inside the same transaction as the DDL, so a
        # half-applied migration can never be recorded as complete.
        _run_script(
            conn,
            path.read_text(),
            f"INSERT INTO meta(key,value) VALUES('schema_version','{version:d}') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value;",
        )
        current = version

    _ensure_fts(conn)


def _ensure_fts(conn):
    """Set up FTS5 if the host sqlite supports it; otherwise record the fallback.

    Text search degrades to LIKE rather than failing -- search quality is worth
    less than the database opening at all.
    """
    if _meta_get(conn, "fts") is not None:
        return
    try:
        _run_script(
            conn,
            FTS_SQL,
            "INSERT INTO issue_fts(rowid, title, body) "
            "SELECT id, title, body FROM issue;"
            "INSERT INTO meta(key,value) VALUES('fts','1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value;",
        )
    except sqlite3.Error:
        _meta_set(conn, "fts", "0")


def has_fts(conn):
    return _meta_get(conn, "fts", "0") == "1"
