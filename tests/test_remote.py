"""RemoteService must be indistinguishable from Service.

The whole point of the remote client is that `cli.py` runs unchanged against
it. These tests drive the same operations both ways and compare, so the two
implementations cannot quietly drift apart.
"""

import contextlib
import io
import json
import threading

from tam import api, cli
from tam.errors import NotFoundError, TransitionError, ValidationError
from tam.remote import RemoteService, token_for

from .base import TamTestCase


class RemoteTestCase(TamTestCase):
    def setUp(self):
        super().setUp()
        self.config.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.token_path.write_text("remote-token\n")
        self.server = api.make_server(self.config, host="127.0.0.1", port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}"
        self.remote = RemoteService(self.url, token="remote-token")


class TestSurfaceParity(TamTestCase):
    """RemoteService must implement everything Service does.

    Regression: `tam_get_experiment` failed remotely because RemoteService was
    missing `latest_metrics`. Comparing the surfaces catches the whole class.
    """

    def test_remote_implements_every_service_method(self):
        from tam.core import Service as LocalService
        from tam.remote import RemoteService as Remote

        def public(cls):
            return {n for n in dir(cls)
                    if not n.startswith("_") and callable(getattr(cls, n))}

        missing = sorted(public(LocalService) - public(Remote))
        self.assertEqual(missing, [],
                         f"RemoteService is missing: {missing}")


class TestRemoteParity(RemoteTestCase):
    def test_latest_metrics_and_events_work_remotely(self):
        issue = self.svc.create_issue(title="measured")
        self.svc.record_metric(issue.key, "recall", 0.7, step=1)
        self.svc.record_metric(issue.key, "recall", 0.9, step=2)
        self.assertEqual(self.remote.latest_metrics(issue.key)["recall"]["value"],
                         0.9)
        self.assertTrue(self.remote.recent_events("2000-01-01T00:00:00Z"))

    def test_params_and_heartbeat_work_remotely(self):
        issue = self.svc.create_issue(title="pushed")
        self.remote.set_params(issue.key, {"lr": 0.001})
        self.assertEqual(self.remote.list_params(issue.key)["lr"], 0.001)
        self.remote.heartbeat(issue.key, "loop", metrics={"recall": 0.5}, step=1)
        self.assertEqual(self.svc.list_watches(issue.key)[0]["provider"],
                         "heartbeat")

    def test_workflow_config_is_fetched_from_the_server(self):
        self.assertEqual(self.remote.config.timezone, self.config.timezone)
        self.assertEqual(self.remote.config.wip_limit, self.config.wip_limit)
        self.assertEqual(self.remote.config.default_project,
                         self.config.default_project)

    def test_create_and_read_match_the_local_service(self):
        local = self.svc.create_issue(title="Local", priority="p1", labels=["a"])
        remote = self.remote.create_issue(title="Remote", priority="p1", labels=["a"])

        def shape(i):
            return (i.title, i.status, i.priority, i.type, i.labels, i.project_key)

        self.assertEqual(shape(self.remote.get_issue(local.key)),
                         shape(self.svc.get_issue(local.key)))
        self.assertEqual(shape(self.svc.get_issue(remote.key)),
                         shape(self.remote.get_issue(remote.key)))

    def test_filters_return_identical_results(self):
        self.svc.create_issue(title="late", due_date="2000-01-01")
        self.svc.create_issue(title="fine", priority="p3")
        self.svc.create_issue(title="tagged", labels=["x", "y"])

        for kw in ({"overdue": True}, {"priority": ("p3",)},
                   {"labels": ("x", "y")}, {"text": "tagged"},
                   {"sort": "key", "limit": 2}):
            with self.subTest(**kw):
                self.assertEqual([i.key for i in self.svc.list_issues(**kw)],
                                 [i.key for i in self.remote.list_issues(**kw)],
                                 f"filter mismatch for {kw}")

    def test_transition_and_guards_behave_the_same(self):
        issue = self.svc.create_issue(title="guarded")
        with self.assertRaises(TransitionError):
            self.remote.transition(issue.key, "done")
        with self.assertRaises(ValidationError):
            self.remote.transition(self.svc.create_issue(
                title="b", status="todo").key, "blocked")
        self.assertEqual(self.remote.transition(issue.key, "todo").status, "todo")

    def test_not_found_maps_back_to_the_right_exception(self):
        with self.assertRaises(NotFoundError):
            self.remote.get_issue("TAM-999")

    def test_comments_links_and_history_round_trip(self):
        a = self.svc.create_issue(title="a")
        b = self.svc.create_issue(title="b")
        self.remote.add_comment(a.key, "hello")
        self.remote.add_link(a.key, b.key, "blocks")

        self.assertEqual([c.body for c in self.remote.list_comments(a.key)], ["hello"])
        self.assertEqual({(l.type, l.to_key) for l in self.remote.list_links(b.key)},
                         {("blocked_by", a.key)})
        self.assertEqual([e.kind for e in self.svc.history(a.key)],
                         [e.kind for e in self.remote.history(a.key)])

    def test_actor_survives_the_network_hop(self):
        issue = self.remote.create_issue(title="attributed", actor="daily-review")
        self.remote.transition(issue.key, "todo", actor="daily-review")
        self.remote.add_comment(issue.key, "note", author="daily-review")
        actors = {e.actor for e in self.svc.history(issue.key)}
        self.assertEqual(actors, {"daily-review"},
                         "remote writes must not be attributed to the server")

    def test_labels_and_stats_match(self):
        self.svc.create_issue(title="x", labels=["one"])
        self.assertEqual(self.remote.list_labels(), self.svc.list_labels())
        self.assertEqual(self.remote.stats(), self.svc.stats())

    def test_projects_match(self):
        self.svc.create_project("OPS", "Ops")
        self.assertEqual([p.key for p in self.remote.list_projects()],
                         [p.key for p in self.svc.list_projects()])
        self.assertEqual(self.remote.get_project("OPS").name, "Ops")
        self.assertIsNotNone(self.remote.archive_project("OPS").archived_at)

    def test_digest_and_review_work_remotely(self):
        self.svc.create_issue(title="late", due_date="2000-01-01")
        payload = self.remote.digest()
        self.assertEqual(len(payload["sections"]["overdue"]), 1)
        date = payload["date"]
        self.assertIsNone(self.remote.digest_run(date))
        self.remote.review(date, "handled")
        self.assertIsNotNone(self.remote.digest_run(date)["reviewed_at"])

    def test_unreachable_api_gives_a_useful_message(self):
        from tam.errors import TamError
        svc = RemoteService.__new__(RemoteService)
        svc.api_url, svc.token, svc.timeout = "http://127.0.0.1:1", None, 2
        with self.assertRaises(TamError) as cm:
            svc._call("GET", "/api/workflow")
        self.assertIn("cannot reach the TAM API", cm.exception.message)


class TestRemoteCli(RemoteTestCase):
    def run_remote(self, *args, expect=0):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = cli.main(["--api", self.url, *args])
        self.assertEqual(code, expect, f"{args} -> {buf.getvalue()}{err.getvalue()}")
        return buf.getvalue()

    def setUp(self):
        super().setUp()
        import os
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {"TAM_API_TOKEN": "remote-token"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cli_runs_unchanged_against_the_api(self):
        out = json.loads(self.run_remote("issue", "create", "-t", "Via remote CLI",
                                         "-p", "p1", "--json"))
        key = out["data"]["key"]
        listed = self.run_remote("issue", "list")
        self.assertIn("Via remote CLI", listed)
        self.assertIn(key, listed)

    def test_remote_errors_keep_their_exit_codes(self):
        out = json.loads(self.run_remote("issue", "show", "TAM-999", "--json", expect=3))
        self.assertEqual(out["error"]["code"], "not_found")

    def test_init_is_refused_over_the_api(self):
        out = json.loads(self.run_remote("init", "--json", expect=4))
        self.assertIn("owner host", out["error"]["message"])

    def test_tree_and_stats_render_remotely(self):
        parent = json.loads(self.run_remote("issue", "create", "-t", "P", "--json")
                            )["data"]["key"]
        self.run_remote("issue", "create", "-t", "C", "--parent", parent, "-q")
        self.assertIn("└ ", self.run_remote("issue", "tree"))
        self.assertIn("total", self.run_remote("stats"))


class TestTokenDiscovery(TamTestCase):
    def test_explicit_token_wins(self):
        self.assertEqual(token_for("http://x", "explicit"), "explicit")

    def test_env_var_is_used(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"TAM_API_TOKEN": "from-env"}):
            self.assertEqual(token_for("http://x"), "from-env")

    def test_falls_back_to_the_token_file_under_tam_home(self):
        import os
        from unittest import mock
        self.config.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.token_path.write_text("from-file\n")
        env = {"TAM_HOME": str(self.root)}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("TAM_API_TOKEN", None)
            self.assertEqual(token_for("http://x"), "from-file")
