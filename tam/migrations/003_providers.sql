-- Generalise watches: any provider, not a fixed list of three.
--
-- The first cut hard-coded kind IN ('slurm','process','path'), which made the
-- schema itself an obstacle to watching anything else. Providers are now named
-- freely and carry their own JSON config, so Encord, GCP, a REST endpoint or a
-- shell command are additions rather than migrations.
--
-- SQLite cannot drop a CHECK constraint in place, so the table is rebuilt.

CREATE TABLE watch_new (
    id           INTEGER PRIMARY KEY,
    issue_id     INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,      -- registered provider name
    ref          TEXT NOT NULL,      -- what to look at, provider-defined
    host         TEXT,
    label        TEXT,
    config       TEXT,               -- provider-specific settings, JSON
    state        TEXT,               -- canonical state
    native_state TEXT,               -- what the provider actually said
    detail       TEXT,
    last_seen    TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (issue_id, provider, ref)
);

INSERT INTO watch_new (id, issue_id, provider, ref, host, label, config,
                       state, native_state, detail, last_seen, created_at)
    SELECT id, issue_id, kind, ref, host, label, NULL,
           NULL, state, detail, last_seen, created_at
    FROM watch;

DROP TABLE watch;
ALTER TABLE watch_new RENAME TO watch;

CREATE INDEX idx_watch_issue    ON watch(issue_id);
CREATE INDEX idx_watch_provider ON watch(provider, ref);
