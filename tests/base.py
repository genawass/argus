"""Shared test fixture: a fresh temp install root per test.

TAM_HOME is patched, not just the database path. `--db` overrides only the
database file, so a test that passed `--db` alone would still resolve
`digest_dir` and the API token against the real install root and write into the
developer's live data directory.

The rest of the TAM_* namespace is cleared for a related reason. Working on a
node means having `TAM_API_URL` and `TAM_API_TOKEN` in your shell -- that is
what `env.sh` is for -- and the CLI treats `TAM_API_URL` as "use the remote
service". A suite that inherited it would silently test the developer's live
API instead of its own fixture, and fail in ways that point nowhere near the
cause. Tests that want remote mode set it themselves.
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
        for name in ("TAM_API_URL", "TAM_API_TOKEN", "TAM_API_TOKEN_FILE",
                     "TAM_ACTOR", "TAM_ALLOW_FOREIGN_DB", "TAM_ENV"):
            os.environ.pop(name, None)      # restored by patcher.stop()
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
