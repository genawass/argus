"""Shared-storage safety.

SQLite on this cluster's NFS cannot be opened from two hosts: measured, WAL lost
100% of the second host's writes and rollback-journal mode lost 11%. The owner
guard is what stops that happening silently, so it is worth real tests.
"""

import os
from unittest import mock

from tam import db
from tam.core import Service, check_db_owner
from tam.errors import ConflictError

from .base import TamTestCase


class TestFilesystemDetection(TamTestCase):
    def test_local_temp_dir_is_not_flagged_as_network(self):
        self.assertFalse(db.is_network_fs(self.config.db_path))

    def test_filesystem_type_resolves_something_for_a_real_path(self):
        self.assertTrue(db.filesystem_type("/"))

    def test_known_network_types_are_flagged(self):
        with mock.patch.object(db, "filesystem_type", return_value="nfs4"):
            self.assertTrue(db.is_network_fs("/anywhere"))
        with mock.patch.object(db, "filesystem_type", return_value="ext4"):
            self.assertFalse(db.is_network_fs("/anywhere"))

    def test_unreadable_mounts_degrade_to_empty(self):
        with mock.patch("pathlib.Path.read_text", side_effect=OSError):
            self.assertEqual(db.filesystem_type("/anywhere"), "")


class TestActorResolution(TamTestCase):
    """Actor names must not need a registry: derive one when nobody chose."""

    def test_derived_default_is_agent_at_hostname(self):
        import socket
        from tam import config as config_mod
        self.assertEqual(config_mod.default_actor(),
                         f"agent@{socket.gethostname()}")

    def test_resolution_order(self):
        import os
        from unittest import mock
        from tam import config as config_mod

        env = dict(os.environ)
        env.pop("TAM_ACTOR", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config_mod.load(root=self.root).actor,
                             config_mod.default_actor())
            with mock.patch.dict(os.environ, {"TAM_ACTOR": "build-agent"}):
                self.assertEqual(config_mod.load(root=self.root).actor,
                                 "build-agent")
                # an explicit argument still wins over the environment
                self.assertEqual(
                    config_mod.load(root=self.root, actor="daily-review").actor,
                    "daily-review")

    def test_legacy_generic_actor_is_treated_as_unset(self):
        """Existing installs stored the literal "agent", which named nobody."""
        import json, os
        from unittest import mock
        from tam import config as config_mod
        cfg = self.config.config_path
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"actor": "agent"}))
        env = dict(os.environ); env.pop("TAM_ACTOR", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config_mod.load(root=self.root).actor,
                             config_mod.default_actor())


class TestOwnerGuard(TamTestCase):
    def setUp(self):
        super().setUp()
        self.net = mock.patch.object(db, "is_network_fs", return_value=True)
        self.net.start()
        self.addCleanup(self.net.stop)

    def test_foreign_host_is_refused(self):
        self.config.db_host = "some-other-host"
        with self.assertRaises(ConflictError) as cm:
            check_db_owner(self.config)
        self.assertIn("some-other-host", cm.exception.message)
        self.assertEqual(cm.exception.details["owner"], "some-other-host")

    def test_owner_host_is_allowed(self):
        import socket
        self.config.db_host = socket.gethostname()
        check_db_owner(self.config)          # must not raise

    def test_no_owner_recorded_means_no_restriction(self):
        self.config.db_host = None
        check_db_owner(self.config)

    def test_local_filesystem_is_never_restricted(self):
        self.config.db_host = "some-other-host"
        with mock.patch.object(db, "is_network_fs", return_value=False):
            check_db_owner(self.config)

    def test_override_env_var_lifts_the_guard(self):
        self.config.db_host = "some-other-host"
        with mock.patch.dict(os.environ, {"TAM_ALLOW_FOREIGN_DB": "1"}):
            check_db_owner(self.config)

    def test_service_construction_enforces_the_guard(self):
        self.config.db_host = "some-other-host"
        with self.assertRaises(ConflictError):
            Service(self.config)

    def test_guard_surfaces_as_a_clean_cli_error_not_a_traceback(self):
        """Regression: Service() used to be built outside main()'s error handler."""
        import io
        import json
        import contextlib
        from tam import cli

        self.config.db_host = "some-other-host"
        self.config.save()
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = cli.main(["--home", str(self.root), "--json", "stats"])
        self.assertEqual(code, 6)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "conflict")
