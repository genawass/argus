"""Shared test fixture: a fresh temp install root per test.

TAM_HOME is patched, not just the database path. `--db` overrides only the
database file, so a test that passed `--db` alone would still resolve
`digest_dir` and the API token against the real install root and write into the
developer's live data directory.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tam import config
from tam.core import Service


class TamTestCase(unittest.TestCase):
    project_key = "TAM"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tam-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"TAM_HOME": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = config.load(root=self.root)
        self.config.actor = "tester"
        self.svc = Service(self.config)
        self.addCleanup(self.svc.close)
        self.svc.create_project(self.project_key, "Test Project")

    def mk(self, title="Thing", **kw):
        return self.svc.create_issue(title=title, **kw)

    def backdate(self, key, updated_at):
        """Rewrite updated_at directly -- the only way to test staleness."""
        self.svc.conn.execute(
            "UPDATE issue SET updated_at=? WHERE key=?", (updated_at, key)
        )

    def advance_to(self, key, status, **kw):
        """Walk an issue through the matrix to reach `status`."""
        path = {
            "todo": ["todo"],
            "in_progress": ["todo", "in_progress"],
            "review": ["todo", "in_progress", "review"],
            "done": ["todo", "in_progress", "done"],
            "blocked": ["todo", "blocked"],
            "cancelled": ["cancelled"],
        }[status]
        issue = None
        for step in path:
            issue = self.svc.transition(key, step, **(kw if step == status else {}))
        return issue
