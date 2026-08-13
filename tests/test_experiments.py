"""Params, heartbeats, and the reporter client.

These cover the push model: work on a machine TAM cannot reach reports into the
central database. The failure mode that matters is a pusher dying silently --
without liveness that is indistinguishable from working quietly.
"""

import sys
import threading
import time
from pathlib import Path
from unittest import mock

from tam import providers
from tam.errors import NotFoundError, ValidationError

from .base import TamTestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients"))
from tam_report import Reporter  # noqa: E402


class TestParams(TamTestCase):
    def test_types_round_trip(self):
        issue = self.mk("run")
        self.svc.set_params(issue.key, {
            "lr": 0.001, "batch": 24, "amp": True, "dataset": "example_dataset",
            "layers": [1, 2, 3],
        })
        stored = self.svc.list_params(issue.key)
        self.assertEqual(stored["lr"], 0.001)
        self.assertEqual(stored["batch"], 24)
        self.assertIs(stored["amp"], True)
        self.assertEqual(stored["dataset"], "example_dataset")
        self.assertEqual(stored["layers"], [1, 2, 3])

    def test_resetting_a_name_overwrites(self):
        issue = self.mk("run")
        self.svc.set_params(issue.key, {"lr": 0.01})
        self.svc.set_params(issue.key, {"lr": 0.001})
        self.assertEqual(self.svc.list_params(issue.key), {"lr": 0.001})

    def test_empty_params_rejected(self):
        issue = self.mk("run")
        with self.assertRaises(ValidationError):
            self.svc.set_params(issue.key, {})

    def test_remove(self):
        issue = self.mk("run")
        self.svc.set_params(issue.key, {"lr": 0.01})
        self.svc.remove_param(issue.key, "lr")
        self.assertEqual(self.svc.list_params(issue.key), {})
        with self.assertRaises(NotFoundError):
            self.svc.remove_param(issue.key, "lr")

    def test_params_are_recorded_in_the_audit_log(self):
        issue = self.mk("run")
        self.svc.set_params(issue.key, {"lr": 0.01}, actor="trainer")
        fields = [(e.field_name, e.actor) for e in self.svc.history(issue.key)]
        self.assertIn(("params", "trainer"), fields)


class TestHeartbeat(TamTestCase):
    def test_first_beat_creates_the_watch(self):
        issue = self.mk("pushed work")
        result = self.svc.heartbeat(issue.key, "train-loop")
        self.assertEqual(result["beats"], 1)
        watches = self.svc.list_watches(issue.key)
        self.assertEqual(watches[0]["provider"], "heartbeat")
        self.assertEqual(watches[0]["ref"], "train-loop")

    def test_beats_accumulate_without_duplicating_watches(self):
        issue = self.mk("pushed work")
        for _ in range(3):
            self.svc.heartbeat(issue.key, "train-loop")
        self.assertEqual(len(self.svc.list_watches(issue.key)), 1)
        self.assertEqual(self.svc.list_watches(issue.key)[0]["detail"]["beats"], 3)

    def test_a_beating_job_scans_as_running(self):
        issue = self.mk("pushed work")
        self.svc.heartbeat(issue.key, "train-loop")
        result = self.svc.scan(key=issue.key)
        self.assertEqual(result["results"][0]["state"], providers.RUNNING)

    def test_silence_becomes_stopped(self):
        """The whole point: a dead pusher must not look like a quiet one."""
        issue = self.mk("pushed work")
        self.svc.heartbeat(issue.key, "train-loop", timeout_seconds=60)
        # Backdate the last beat past its timeout.
        self.svc.conn.execute(
            "UPDATE watch SET detail = json_set(detail, '$.last_beat',"
            " '2000-01-01T00:00:00Z') WHERE issue_id ="
            " (SELECT id FROM issue WHERE key=?)", (issue.key,))
        result = self.svc.scan(key=issue.key)
        self.assertEqual(result["results"][0]["state"], providers.STOPPED)
        self.assertEqual(result["results"][0]["native_state"], "silent")
        self.assertEqual(len(result["attention"]), 1)

    def test_a_reported_ending_sticks_and_is_not_silence(self):
        issue = self.mk("pushed work")
        self.svc.heartbeat(issue.key, "train-loop", final_state="succeeded")
        self.svc.conn.execute(
            "UPDATE watch SET detail = json_set(detail, '$.last_beat',"
            " '2000-01-01T00:00:00Z') WHERE issue_id ="
            " (SELECT id FROM issue WHERE key=?)", (issue.key,))
        result = self.svc.scan(key=issue.key)
        self.assertEqual(result["results"][0]["state"], providers.SUCCEEDED)
        self.assertEqual(result["attention"], [])

    def test_never_reported_is_pending_not_stopped(self):
        issue = self.mk("pushed work")
        self.svc.add_watch(issue.key, "heartbeat", "train-loop")
        result = self.svc.scan(key=issue.key)
        self.assertEqual(result["results"][0]["state"], providers.PENDING)

    def test_beat_carries_metrics_and_params(self):
        issue = self.mk("pushed work")
        self.svc.heartbeat(issue.key, "train-loop", metrics={"recall": 0.81},
                           step=5, params={"lr": 0.001})
        self.assertAlmostEqual(
            self.svc.latest_metrics(issue.key)["recall"]["value"], 0.81)
        self.assertEqual(self.svc.list_params(issue.key)["lr"], 0.001)

    def test_invalid_final_state_rejected(self):
        issue = self.mk("pushed work")
        with self.assertRaises(ValidationError):
            self.svc.heartbeat(issue.key, "run", final_state="COMPLETED")

    def test_separate_refs_are_separate_watches(self):
        issue = self.mk("pushed work")
        self.svc.heartbeat(issue.key, "trainer")
        self.svc.heartbeat(issue.key, "evaluator")
        self.assertEqual(len(self.svc.list_watches(issue.key)), 2)


class TestReporterClient(TamTestCase):
    """The client must never break the job it reports on."""

    def setUp(self):
        super().setUp()
        self.calls = []

    def reporter(self, **kw):
        r = Reporter("TAM-1", url="http://tam.invalid", token="t", quiet=True, **kw)
        r._post = lambda path, body: self.calls.append((path, body)) or {"ok": True}
        return r

    def test_disabled_without_a_url(self):
        r = Reporter("TAM-1", url="", quiet=True)
        self.assertFalse(r.enabled)
        self.assertIsNone(r.params(lr=1))
        self.assertIsNone(r.metric(recall=0.5))

    def test_params_and_metrics_post_expected_bodies(self):
        r = self.reporter()
        r.params(lr=0.001, batch=24)
        r.metric(step=3, recall=0.8, precision=0.7)
        (p1, b1), (p2, b2) = self.calls
        self.assertTrue(p1.endswith("/params"))
        self.assertEqual(b1["params"], {"lr": 0.001, "batch": 24})
        self.assertTrue(p2.endswith("/heartbeat"))
        self.assertEqual(b2["step"], 3)
        self.assertEqual(b2["metrics"], {"recall": 0.8, "precision": 0.7})

    def test_none_metrics_are_dropped_not_sent_as_null(self):
        r = self.reporter()
        r.metric(step=1, recall=0.8, missing=None)
        self.assertEqual(self.calls[0][1]["metrics"], {"recall": 0.8})

    def test_context_manager_reports_success(self):
        r = self.reporter()
        with r:
            pass
        self.assertEqual(self.calls[-1][1]["final_state"], "succeeded")

    def test_context_manager_reports_failure_and_reraises(self):
        r = self.reporter()
        with self.assertRaises(RuntimeError):
            with r:
                raise RuntimeError("training blew up")
        body = self.calls[-1][1]
        self.assertEqual(body["final_state"], "failed")
        self.assertIn("training blew up", body["note"])

    def test_network_errors_never_reach_the_caller(self):
        r = Reporter("TAM-1", url="http://127.0.0.1:1", token="t", quiet=True,
                     timeout=1)
        self.assertIsNone(r.metric(step=1, recall=0.5))   # must not raise
        self.assertGreater(r.failures, 0)

    def test_background_heartbeat_thread_stops_on_done(self):
        r = self.reporter(heartbeat_seconds=0.05)
        time.sleep(0.16)
        r.done()
        beats = len(self.calls)
        time.sleep(0.15)
        self.assertGreaterEqual(beats, 2, "background thread should have beaten")
        self.assertEqual(len(self.calls), beats, "thread must stop after done()")
        self.assertNotIn("tam-heartbeat",
                         [t.name for t in threading.enumerate() if t.is_alive()
                          and t.name == "tam-heartbeat" and not t.daemon])
