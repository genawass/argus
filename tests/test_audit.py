"""Audit completeness, updates, comments, and migration/concurrency behaviour."""

import threading

from tam import config, db
from tam.core import Service
from tam.errors import ValidationError

from .base import TamTestCase


class TestUpdates(TamTestCase):
    def test_each_changed_field_records_an_event(self):
        issue = self.mk("original", priority="p2")
        self.svc.update_issue(issue.key, title="renamed", priority="p0")
        changes = {
            (e.field_name, e.old_value, e.new_value)
            for e in self.svc.history(issue.key)
            if e.kind == "updated"
        }
        self.assertIn(("title", "original", "renamed"), changes)
        self.assertIn(("priority", "p2", "p0"), changes)

    def test_unchanged_fields_record_nothing(self):
        issue = self.mk("same", priority="p2")
        self.svc.update_issue(issue.key, title="same", priority="p2")
        self.assertEqual([e for e in self.svc.history(issue.key) if e.kind == "updated"], [])

    def test_nullable_fields_can_be_cleared(self):
        issue = self.mk("x", due_date="2026-01-01", assignee="me")
        updated = self.svc.update_issue(issue.key, due_date=None, assignee=None)
        self.assertIsNone(updated.due_date)
        self.assertIsNone(updated.assignee)

    def test_label_changes_are_recorded_as_one_event(self):
        issue = self.mk("x", labels=["a"])
        updated = self.svc.add_labels(issue.key, ["b"])
        self.assertEqual(sorted(updated.labels), ["a", "b"])
        events = [e for e in self.svc.history(issue.key) if e.field_name == "labels"]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].old_value, events[0].new_value), ("a", "a,b"))
        self.assertEqual(self.svc.remove_labels(issue.key, ["a"]).labels, ("b",))

    def test_unknown_field_is_rejected(self):
        issue = self.mk("x")
        with self.assertRaises(ValidationError):
            self.svc.update_issue(issue.key, nonexistent="value")

    def test_updates_touch_updated_at(self):
        issue = self.mk("x")
        self.backdate(issue.key, "2000-01-01T00:00:00Z")
        self.svc.update_issue(issue.key, title="new")
        self.assertGreater(self.svc.get_issue(issue.key).updated_at, "2020")

    def test_actor_is_recorded_and_overridable(self):
        issue = self.mk("x")
        self.svc.update_issue(issue.key, title="y", actor="daily-review")
        actors = {e.actor for e in self.svc.history(issue.key)}
        self.assertEqual(actors, {"tester", "daily-review"})


class TestCommentsAndHistory(TamTestCase):
    def test_comments_round_trip_and_touch_updated_at(self):
        issue = self.mk("x")
        self.backdate(issue.key, "2000-01-01T00:00:00Z")
        self.svc.add_comment(issue.key, "first note")
        comments = self.svc.list_comments(issue.key)
        self.assertEqual([c.body for c in comments], ["first note"])
        self.assertGreater(self.svc.get_issue(issue.key).updated_at, "2020")

    def test_empty_comment_is_rejected(self):
        issue = self.mk("x")
        with self.assertRaises(ValidationError):
            self.svc.add_comment(issue.key, "   ")

    def test_history_survives_deletion(self):
        issue = self.mk("doomed")
        self.svc.add_comment(issue.key, "note")
        self.svc.delete_issue(issue.key)
        kinds = [e.kind for e in self.svc.history(issue.key)]
        self.assertIn("created", kinds)
        self.assertIn("deleted", kinds)

    def test_delete_reports_orphaned_subtasks(self):
        parent = self.mk("parent")
        child = self.mk("child", parent=parent.key)
        result = self.svc.delete_issue(parent.key)
        self.assertEqual(result["orphaned_subtasks"], [child.key])
        self.assertIsNone(self.svc.get_issue(child.key).parent_key)


class TestDatabase(TamTestCase):
    def test_migrations_are_idempotent(self):
        version = lambda: self.svc.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        before = version()
        db.migrate(self.svc.conn)
        db.migrate(self.svc.conn)
        self.assertEqual(version(), before)

    def test_reopening_an_existing_database_preserves_data(self):
        issue = self.mk("persisted")
        self.svc.close()
        reopened = Service(config.load(root=self.root))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_issue(issue.key).title, "persisted")

    def test_concurrent_writers_do_not_collide_on_keys(self):
        errors, keys = [], []
        lock = threading.Lock()

        def worker(n):
            try:
                svc = Service(config.load(root=self.root))
                try:
                    for i in range(5):
                        issue = svc.create_issue(title=f"w{n}-{i}")
                        with lock:
                            keys.append(issue.key)
                finally:
                    svc.close()
            except Exception as exc:  # noqa: BLE001 - surfaced via assertion below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(keys), 20)
        self.assertEqual(len(set(keys)), 20, "issue keys must be unique under concurrency")

    def test_failed_transaction_leaves_no_partial_issue(self):
        before = self.svc.count_issues(include_closed=True)
        with self.assertRaises(ValidationError):
            self.mk("bad", due_date="not-a-date")
        self.assertEqual(self.svc.count_issues(include_closed=True), before)
