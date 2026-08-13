"""Watches, metrics, targets, the scanner, and backups."""

import sqlite3
from unittest import mock

from tam import backup as backup_mod
from tam import parsers, providers
from tam import scan as scan_mod
from tam.errors import NotFoundError, ValidationError

from .base import TamTestCase


class TestWatches(TamTestCase):
    def test_add_and_list(self):
        issue = self.mk("watched")
        self.svc.add_watch(issue.key, "slurm", "12345", label="train")
        watches = self.svc.list_watches(issue.key)
        self.assertEqual(len(watches), 1)
        self.assertEqual((watches[0]["provider"], watches[0]["ref"]),
                         ("slurm", "12345"))

    def test_duplicate_watch_is_ignored(self):
        issue = self.mk("watched")
        self.svc.add_watch(issue.key, "slurm", "1")
        self.svc.add_watch(issue.key, "slurm", "1")
        self.assertEqual(len(self.svc.list_watches(issue.key)), 1)

    def test_unknown_provider_rejected(self):
        issue = self.mk("watched")
        with self.assertRaises(ValidationError):
            self.svc.add_watch(issue.key, "telepathy", "1")

    def test_remove(self):
        issue = self.mk("watched")
        wid = self.svc.add_watch(issue.key, "path", "/tmp/x")[0]["id"]
        self.svc.remove_watch(wid)
        self.assertEqual(self.svc.list_watches(issue.key), [])
        with self.assertRaises(NotFoundError):
            self.svc.remove_watch(wid)

    def test_deleting_the_issue_removes_its_watches(self):
        issue = self.mk("doomed")
        self.svc.add_watch(issue.key, "slurm", "9")
        self.svc.delete_issue(issue.key)
        self.assertEqual(self.svc.list_watches(), [])

    def test_filter_by_provider(self):
        issue = self.mk("w")
        self.svc.add_watch(issue.key, "slurm", "1")
        self.svc.add_watch(issue.key, "path", "/tmp")
        self.assertEqual(len(self.svc.list_watches(provider="slurm")), 1)

    def test_config_round_trips(self):
        issue = self.mk("w")
        self.svc.add_watch(issue.key, "slurm", "7",
                           config={"parser": "yolo", "log": "/tmp/a.out"})
        self.assertEqual(self.svc.list_watches(issue.key)[0]["config"]["parser"],
                         "yolo")


class TestMetrics(TamTestCase):
    def test_record_and_list(self):
        issue = self.mk("m")
        self.svc.record_metric(issue.key, "recall", 0.8, step=1)
        self.svc.record_metric(issue.key, "recall", 0.82, step=2)
        points = self.svc.list_metrics(issue.key, name="recall")
        self.assertEqual([p["value"] for p in points], [0.8, 0.82])

    def test_rescanning_the_same_step_updates_rather_than_duplicates(self):
        issue = self.mk("m")
        self.svc.record_metric(issue.key, "recall", 0.80, step=5, source="slurm:1")
        self.svc.record_metric(issue.key, "recall", 0.81, step=5, source="slurm:1")
        points = self.svc.list_metrics(issue.key, name="recall")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["value"], 0.81)

    def test_non_numeric_value_rejected(self):
        issue = self.mk("m")
        with self.assertRaises(ValidationError):
            self.svc.record_metric(issue.key, "recall", "high")

    def test_latest_metrics(self):
        issue = self.mk("m")
        self.svc.record_metric(issue.key, "recall", 0.7, step=1)
        self.svc.record_metric(issue.key, "recall", 0.9, step=2)
        self.svc.record_metric(issue.key, "map50", 0.5, step=2)
        latest = self.svc.latest_metrics(issue.key)
        self.assertEqual(latest["recall"]["value"], 0.9)
        self.assertEqual(latest["map50"]["value"], 0.5)

    def test_noise_is_not_reported_as_a_trend(self):
        """Regression: endpoint comparison called an oscillation "rising".

        These are recall values from a real run; the endpoint delta is +0.010
        while the spread is 0.016, so no direction is supportable.
        """
        issue = self.mk("m")
        for i, v in enumerate([0.793, 0.791, 0.800, 0.805, 0.807, 0.798, 0.803]):
            self.svc.record_metric(issue.key, "recall", v, step=i)
        trend = self.svc.metric_trend(issue.key, "recall")
        self.assertEqual(trend["verdict"], "flat")
        self.assertTrue(trend["noise_dominated"])
        self.assertGreater(trend["delta"], 0, "endpoints do rise")
        self.assertAlmostEqual(trend["stdev"], 0.0056, places=3)

    def test_a_real_trend_survives_noise(self):
        issue = self.mk("m")
        for i, v in enumerate([0.50, 0.53, 0.51, 0.58, 0.60, 0.59, 0.65, 0.68]):
            self.svc.record_metric(issue.key, "map50", v, step=i)
        trend = self.svc.metric_trend(issue.key, "map50")
        self.assertEqual(trend["verdict"], "rising")
        self.assertFalse(trend["noise_dominated"])
        self.assertGreater(trend["change_over_window"], 0)

    def test_falling_is_detected(self):
        issue = self.mk("m")
        for i, v in enumerate([0.90, 0.86, 0.84, 0.79, 0.75, 0.71]):
            self.svc.record_metric(issue.key, "acc", v, step=i)
        self.assertEqual(self.svc.metric_trend(issue.key, "acc")["verdict"],
                         "falling")

    def test_trend_detects_flat_and_rising(self):
        issue = self.mk("m")
        for i in range(10):
            self.svc.record_metric(issue.key, "flatline", 0.80, step=i)
        for i in range(10):
            self.svc.record_metric(issue.key, "climbing", 0.5 + i * 0.05, step=i)
        self.assertTrue(self.svc.metric_trend(issue.key, "flatline")["flat"])
        rising = self.svc.metric_trend(issue.key, "climbing")
        self.assertFalse(rising["flat"])
        self.assertGreater(rising["delta"], 0)

    def test_trend_needs_two_points(self):
        issue = self.mk("m")
        self.svc.record_metric(issue.key, "x", 1.0, step=1)
        self.assertIsNone(self.svc.metric_trend(issue.key, "x"))


class TestTargets(TamTestCase):
    def test_evaluate_met_and_unmet(self):
        issue = self.mk("t")
        self.svc.set_target(issue.key, "recall", ">=", 0.90)
        self.assertEqual(self.svc.evaluate_targets(issue.key)[0]["status"], "no data")

        self.svc.record_metric(issue.key, "recall", 0.80, step=1)
        row = self.svc.evaluate_targets(issue.key)[0]
        self.assertEqual(row["status"], "not met")
        self.assertAlmostEqual(row["gap"], 0.10, places=6)

        self.svc.record_metric(issue.key, "recall", 0.95, step=2)
        self.assertEqual(self.svc.evaluate_targets(issue.key)[0]["status"], "met")

    def test_less_than_targets(self):
        issue = self.mk("t")
        self.svc.set_target(issue.key, "far", "<", 10)
        self.svc.record_metric(issue.key, "far", 4.0, step=1)
        self.assertTrue(self.svc.evaluate_targets(issue.key)[0]["met"])

    def test_target_falls_back_to_the_parent_metric(self):
        """The job is watched on the parent; the criterion lives on the subtask."""
        parent = self.mk("Model A")
        child = self.mk("Accuracy above 90", parent=parent.key)
        self.svc.set_target(child.key, "recall", ">=", 0.90)
        self.svc.record_metric(parent.key, "recall", 0.79, step=80)

        row = self.svc.evaluate_targets(child.key)[0]
        self.assertEqual(row["status"], "not met")
        self.assertAlmostEqual(row["actual"], 0.79)
        self.assertEqual(row["metric_from"], parent.key)

    def test_own_metric_beats_the_parent(self):
        parent = self.mk("P")
        child = self.mk("C", parent=parent.key)
        self.svc.set_target(child.key, "recall", ">=", 0.9)
        self.svc.record_metric(parent.key, "recall", 0.5, step=1)
        self.svc.record_metric(child.key, "recall", 0.95, step=1)
        row = self.svc.evaluate_targets(child.key)[0]
        self.assertTrue(row["met"])
        self.assertEqual(row["metric_from"], child.key)

    def test_setting_twice_updates(self):
        issue = self.mk("t")
        self.svc.set_target(issue.key, "recall", ">=", 0.8)
        self.svc.set_target(issue.key, "recall", ">=", 0.9)
        targets = self.svc.list_targets(issue.key)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["value"], 0.9)

    def test_bad_operator_rejected(self):
        issue = self.mk("t")
        with self.assertRaises(ValidationError):
            self.svc.set_target(issue.key, "recall", "≈", 0.9)

    def test_remove(self):
        issue = self.mk("t")
        self.svc.set_target(issue.key, "recall", ">=", 0.9)
        self.svc.remove_target(issue.key, "recall")
        self.assertEqual(self.svc.list_targets(issue.key), [])
        with self.assertRaises(NotFoundError):
            self.svc.remove_target(issue.key, "recall")


def fake_probe(state, metrics=None, step=None, native=None):
    from tam.providers import Observation
    return lambda watch: Observation(state, native_state=native,
                                     detail={}, metrics=metrics or {}, step=step)


class TestScan(TamTestCase):
    def test_state_change_is_recorded_and_commented(self):
        issue = self.mk("job")
        self.svc.add_watch(issue.key, "slurm", "111")

        with mock.patch.object(providers.get("slurm"), "probe",
                               fake_probe("running")):
            first = self.svc.scan()
        self.assertEqual(first["results"][0]["state"], "running")

        with mock.patch.object(providers.get("slurm"), "probe",
                               fake_probe("failed", native="OUT_OF_MEMORY")):
            second = self.svc.scan()
        self.assertTrue(second["results"][0]["changed"])
        bodies = [c.body for c in self.svc.list_comments(issue.key)]
        self.assertTrue(any("running -> failed" in b for b in bodies))

    def test_unchanged_state_does_not_comment(self):
        issue = self.mk("job")
        self.svc.add_watch(issue.key, "slurm", "111")
        with mock.patch.object(providers.get("slurm"), "probe",
                               fake_probe("running")):
            self.svc.scan()
            self.svc.scan()
            self.svc.scan()
        self.assertEqual(len(self.svc.list_comments(issue.key)), 0)

    def test_metrics_are_recorded_from_the_job_log(self):
        issue = self.mk("job")
        self.svc.add_watch(issue.key, "slurm", "222")
        parsed = {"precision": 0.78, "recall": 0.80, "map50": 0.83,
                  "map50_95": 0.62}
        with mock.patch.object(providers.get("slurm"), "probe",
                               fake_probe("running", metrics=parsed)):
            result = self.svc.scan()
        self.assertEqual(result["results"][0]["metrics"], parsed)
        self.assertAlmostEqual(self.svc.latest_metrics(issue.key)["recall"]["value"], 0.80)

    def test_repeated_scans_do_not_grow_the_series(self):
        """Regression: a 15-minute timer against an idle log bloated the table."""
        issue = self.mk("job")
        self.svc.add_watch(issue.key, "slurm", "333")
        parsed = {"precision": 0.78, "recall": 0.80, "map50": 0.83,
                  "map50_95": 0.62}
        with mock.patch.object(providers.get("slurm"), "probe",
                               fake_probe("running", metrics=parsed, step=80)):
            for _ in range(5):
                self.svc.scan()
        self.assertEqual(len(self.svc.list_metrics(issue.key)), 4)

        moved = {**parsed, "recall": 0.85}
        with mock.patch.object(providers.get("slurm"), "probe",
                               fake_probe("running", metrics=moved, step=81)):
            self.svc.scan()
        self.assertAlmostEqual(
            self.svc.latest_metrics(issue.key)["recall"]["value"], 0.85)

    def test_scan_can_be_limited_to_one_issue(self):
        a, b = self.mk("a"), self.mk("b")
        self.svc.add_watch(a.key, "slurm", "1")
        self.svc.add_watch(b.key, "slurm", "2")
        with mock.patch.object(providers.get("slurm"), "probe",
                               fake_probe("running")):
            self.assertEqual(self.svc.scan(key=a.key)["watches"], 1)

    def test_scan_with_no_watches_is_harmless(self):
        self.assertEqual(self.svc.scan()["watches"], 0)


class TestParsers(TamTestCase):
    LOG = (
        "               Class      Images      Labels           P           R\n"
        "                 all       10032       12526       0.775       0.804       0.830       0.626\n"
        "    80/299     14.5G   0.013   0.001\n"
        "                 all       10032       12526       0.784       0.799       0.832       0.629\n"
    )

    def test_yolo_takes_the_most_recent_row_and_step(self):
        metrics, step = parsers.extract(self.LOG, {"parser": "yolo"})
        self.assertAlmostEqual(metrics["precision"], 0.784)
        self.assertAlmostEqual(metrics["recall"], 0.799)
        self.assertEqual(step, 80)

    def test_yolo_returns_nothing_when_the_format_does_not_match(self):
        self.assertEqual(parsers.extract("nothing here", {"parser": "yolo"}), ({}, None))

    def test_regex_parser(self):
        metrics, step = parsers.extract(
            "loss=0.51\nloss=0.42\nepoch 7\n",
            {"parser": "regex", "patterns": {"loss": r"loss=([0-9.]+)"},
             "step_pattern": r"epoch (\d+)"})
        self.assertAlmostEqual(metrics["loss"], 0.42)
        self.assertEqual(step, 7)

    def test_json_parser(self):
        doc = '{"metrics": {"recall": 0.61}, "epoch": 12}'
        metrics, step = parsers.extract(
            doc, {"parser": "json", "paths": {"recall": "metrics.recall"},
                  "step_path": "epoch"})
        self.assertAlmostEqual(metrics["recall"], 0.61)
        self.assertEqual(step, 12)

    def test_unknown_parser_is_rejected(self):
        with self.assertRaises(ValidationError):
            parsers.extract("x", {"parser": "telepathy"})

    def test_no_parser_configured_is_a_no_op(self):
        self.assertEqual(parsers.extract("x", {}), ({}, None))


class TestBackup(TamTestCase):
    def test_backup_is_readable_and_complete(self):
        for i in range(3):
            self.mk(f"issue {i}")
        result = self.svc.backup()
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(result["issues"], 3)

        conn = sqlite3.connect(result["path"])
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM issue").fetchone()[0], 3)
        finally:
            conn.close()

    def test_retention_prunes_oldest(self):
        self.mk("x")
        dest = self.root / "bk"
        for i in range(5):
            # timestamps have second resolution; name them apart explicitly
            r = backup_mod.run(self.config, dest=dest, keep=99)
            import os
            os.rename(r["path"], dest / f"tam-2026010{i}T000000Z.db")
        result = backup_mod.run(self.config, dest=dest, keep=3)
        remaining = sorted(p.name for p in dest.glob("tam-*.db"))
        self.assertEqual(len(remaining), 3)
        self.assertIn(result["path"].rsplit("/", 1)[-1], remaining)

    def test_backup_captures_committed_data_only(self):
        issue = self.mk("committed")
        result = self.svc.backup()
        conn = sqlite3.connect(result["path"])
        try:
            keys = [r[0] for r in conn.execute("SELECT key FROM issue")]
        finally:
            conn.close()
        self.assertEqual(keys, [issue.key])
