"""Transition matrix, guards, and key allocation."""

from tam.core.workflow import TRANSITIONS
from tam.errors import ConflictError, NotFoundError, TransitionError, ValidationError
from tam.models import STATUSES

from .base import TamTestCase


class TestKeys(TamTestCase):
    def test_keys_are_sequential_per_project(self):
        keys = [self.mk(f"t{i}").key for i in range(3)]
        self.assertEqual(keys, ["TAM-1", "TAM-2", "TAM-3"])

    def test_keys_are_not_reused_after_delete(self):
        a = self.mk("first")
        self.svc.delete_issue(a.key)
        b = self.mk("second")
        self.assertEqual(b.key, "TAM-2")
        with self.assertRaises(NotFoundError):
            self.svc.get_issue("TAM-1")

    def test_separate_projects_have_separate_counters(self):
        self.svc.create_project("OPS", "Ops")
        self.assertEqual(self.mk("a").key, "TAM-1")
        self.assertEqual(self.mk("b", project="OPS").key, "OPS-1")

    def test_malformed_key_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.get_issue("not-a-key")

    def test_cannot_create_in_archived_project(self):
        self.svc.archive_project("TAM")
        with self.assertRaises(ConflictError):
            self.mk("nope")


class TestTransitions(TamTestCase):
    def test_every_declared_transition_is_legal(self):
        for src, targets in TRANSITIONS.items():
            for dst in targets:
                issue = self.mk(f"{src}->{dst}", status=src, reason="seed")
                result = self.svc.transition(
                    issue.key, dst, reason="because" if dst == "blocked" else None
                )
                self.assertEqual(result.status, dst, f"{src} -> {dst}")

    def test_undeclared_transitions_are_rejected(self):
        for src in TRANSITIONS:
            for dst in STATUSES:
                if dst == src or dst in TRANSITIONS[src]:
                    continue
                issue = self.mk(f"{src}!{dst}", status=src, reason="seed")
                with self.assertRaises(TransitionError, msg=f"{src} -> {dst}"):
                    self.svc.transition(issue.key, dst, reason="r")

    def test_same_status_is_idempotent(self):
        issue = self.mk("x", status="todo")
        again = self.svc.transition(issue.key, "todo")
        self.assertEqual(again.status, "todo")
        kinds = [e.kind for e in self.svc.history(issue.key)]
        self.assertEqual(kinds.count("transitioned"), 0)

    def test_closing_sets_and_reopening_clears_closed_at(self):
        issue = self.advance_to(self.mk("x").key, "done")
        self.assertIsNotNone(issue.closed_at)
        reopened = self.svc.transition(issue.key, "in_progress")
        self.assertIsNone(reopened.closed_at)


class TestGuards(TamTestCase):
    def test_open_subtasks_block_closure(self):
        parent = self.mk("parent")
        self.mk("child", parent=parent.key)
        self.svc.transition(parent.key, "todo")
        self.svc.transition(parent.key, "in_progress")
        with self.assertRaises(TransitionError) as cm:
            self.svc.transition(parent.key, "done")
        self.assertIn("TAM-2", cm.exception.details["open_subtasks"])

    def test_force_overrides_subtask_guard_and_is_recorded(self):
        parent = self.mk("parent")
        self.mk("child", parent=parent.key)
        self.svc.transition(parent.key, "todo")
        self.svc.transition(parent.key, "in_progress")
        done = self.svc.transition(parent.key, "done", force=True)
        self.assertEqual(done.status, "done")
        note = [e.note for e in self.svc.history(parent.key) if e.kind == "transitioned"][-1]
        self.assertIn("forced", note)

    def test_closed_subtasks_do_not_block_closure(self):
        parent = self.mk("parent")
        child = self.mk("child", parent=parent.key)
        self.advance_to(child.key, "cancelled")
        self.svc.transition(parent.key, "todo")
        self.svc.transition(parent.key, "in_progress")
        self.assertEqual(self.svc.transition(parent.key, "done").status, "done")

    def test_blocking_requires_a_reason(self):
        issue = self.mk("x", status="todo")
        with self.assertRaises(ValidationError):
            self.svc.transition(issue.key, "blocked")
        self.assertEqual(
            self.svc.transition(issue.key, "blocked", reason="waiting on vendor").status,
            "blocked",
        )

    def test_creating_directly_as_blocked_requires_a_reason(self):
        with self.assertRaises(ValidationError):
            self.mk("stuck from birth", status="blocked")
        issue = self.mk("stuck", status="blocked", reason="waiting on vendor")
        self.assertEqual(issue.status, "blocked")
        note = [e.note for e in self.svc.history(issue.key) if e.kind == "created"][0]
        self.assertEqual(note, "waiting on vendor")

    def test_blocked_by_link_satisfies_the_reason_guard(self):
        issue = self.mk("x", status="todo")
        blocker = self.mk("blocker")
        self.svc.add_link(issue.key, blocker.key, "blocked_by")
        self.assertEqual(self.svc.transition(issue.key, "blocked").status, "blocked")

    def test_subtask_nesting_is_one_level(self):
        parent = self.mk("parent")
        child = self.mk("child", parent=parent.key)
        with self.assertRaises(ValidationError):
            self.mk("grandchild", parent=child.key)

    def test_issue_with_subtasks_cannot_become_a_subtask(self):
        parent = self.mk("parent")
        self.mk("child", parent=parent.key)
        other = self.mk("other")
        with self.assertRaises(ValidationError):
            self.svc.update_issue(parent.key, parent=other.key)

    def test_status_cannot_be_changed_through_update(self):
        issue = self.mk("x")
        with self.assertRaises(ValidationError):
            self.svc.update_issue(issue.key, status="done")
