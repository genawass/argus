-- Turn TAM from a record of work into something that watches it.
--
-- watch  : binds an issue to something observable (a Slurm job, a process on a
--          host, a directory that should be growing). Until this existed, the
--          job -> issue mapping lived only in free-text comments and nothing
--          could be automated.
-- metric : numeric observations over time, so "recall is flat" is a query
--          rather than something a human eyeballs in a log.
-- target : acceptance criteria as data, so "Accuracy above 90" can be evaluated.

CREATE TABLE watch (
    id         INTEGER PRIMARY KEY,
    issue_id   INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('slurm', 'process', 'path')),
    ref        TEXT NOT NULL,     -- job id / pgrep pattern / filesystem path
    host       TEXT,              -- where to look, for process and path kinds
    label      TEXT,
    state      TEXT,              -- last observed state
    detail     TEXT,              -- last observation, JSON
    last_seen  TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (issue_id, kind, ref)
);
CREATE INDEX idx_watch_issue ON watch(issue_id);
CREATE INDEX idx_watch_kind  ON watch(kind, ref);

CREATE TABLE metric (
    id       INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    value    REAL NOT NULL,
    step     INTEGER,             -- epoch / iteration, when meaningful
    source   TEXT,
    at       TEXT NOT NULL
);
CREATE INDEX idx_metric_issue ON metric(issue_id, name, at);
-- Re-scanning a log must not pile up duplicate points for the same step.
CREATE UNIQUE INDEX idx_metric_dedup
    ON metric(issue_id, name, step, source) WHERE step IS NOT NULL;

CREATE TABLE target (
    id         INTEGER PRIMARY KEY,
    issue_id   INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    metric     TEXT NOT NULL,
    op         TEXT NOT NULL CHECK (op IN ('>=', '>', '<=', '<', '==')),
    value      REAL NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (issue_id, metric)
);
CREATE INDEX idx_target_issue ON target(issue_id);
