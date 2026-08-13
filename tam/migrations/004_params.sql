-- Run parameters: the other half of an experiment.
--
-- Metrics answer "what did it score"; params answer "what was it configured
-- with". Without these, hyperparameters live in the issue body as prose and
-- "which learning rate produced the best recall" is unanswerable.
--
-- Values are stored as text with a declared type, so ints, floats, booleans
-- and strings all round-trip without a column per kind.

CREATE TABLE param (
    id       INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    value    TEXT NOT NULL,
    type     TEXT NOT NULL DEFAULT 'str'
             CHECK (type IN ('str', 'int', 'float', 'bool', 'json')),
    source   TEXT,
    at       TEXT NOT NULL,
    UNIQUE (issue_id, name)
);
CREATE INDEX idx_param_issue ON param(issue_id);
CREATE INDEX idx_param_name  ON param(name);
