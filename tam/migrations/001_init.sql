-- TAM schema v1.
-- CHECK constraints mirror the enums in models.py. Core validates first and
-- produces friendly errors; these are the backstop that keeps the file honest
-- if anything ever writes to it directly.

CREATE TABLE project (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    issue_seq   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE issue (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    key          TEXT NOT NULL UNIQUE,
    type         TEXT NOT NULL DEFAULT 'task'
                 CHECK (type IN ('task','bug','chore','spike')),
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'backlog'
                 CHECK (status IN ('backlog','todo','in_progress','blocked',
                                   'review','done','cancelled')),
    priority     TEXT NOT NULL DEFAULT 'p2'
                 CHECK (priority IN ('p0','p1','p2','p3')),
    assignee     TEXT,
    reporter     TEXT,
    due_date     TEXT,
    parent_id    INTEGER REFERENCES issue(id) ON DELETE SET NULL,
    external_ref TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    closed_at    TEXT,
    UNIQUE (project_id, seq)
);

CREATE INDEX idx_issue_status      ON issue(status);
CREATE INDEX idx_issue_due         ON issue(due_date);
CREATE INDEX idx_issue_updated     ON issue(updated_at);
CREATE INDEX idx_issue_parent      ON issue(parent_id);
CREATE INDEX idx_issue_proj_status ON issue(project_id, status);

CREATE TABLE comment (
    id         INTEGER PRIMARY KEY,
    issue_id   INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_comment_issue ON comment(issue_id, created_at);

CREATE TABLE label (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE issue_label (
    issue_id INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES label(id) ON DELETE CASCADE,
    PRIMARY KEY (issue_id, label_id)
);
CREATE INDEX idx_issue_label_label ON issue_label(label_id);

CREATE TABLE issue_link (
    id         INTEGER PRIMARY KEY,
    from_issue INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    to_issue   INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    type       TEXT NOT NULL
               CHECK (type IN ('blocks','blocked_by','relates_to',
                               'duplicates','duplicated_by')),
    created_at TEXT NOT NULL,
    UNIQUE (from_issue, to_issue, type),
    CHECK (from_issue <> to_issue)
);
CREATE INDEX idx_link_from ON issue_link(from_issue);
CREATE INDEX idx_link_to   ON issue_link(to_issue);

-- Append-only. Rows survive deletion of the issue they describe, which is the
-- whole point: "what happened to TAM-42" must remain answerable.
CREATE TABLE event (
    id        INTEGER PRIMARY KEY,
    issue_id  INTEGER,
    issue_key TEXT,
    actor     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    field     TEXT,
    old_value TEXT,
    new_value TEXT,
    note      TEXT,
    at        TEXT NOT NULL
);
CREATE INDEX idx_event_issue ON event(issue_id, at);
CREATE INDEX idx_event_at    ON event(at);

CREATE TABLE digest_run (
    id           INTEGER PRIMARY KEY,
    run_date     TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    reviewed_at  TEXT,
    review_notes TEXT
);
