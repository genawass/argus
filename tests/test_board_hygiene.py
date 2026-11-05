"""Backward moves, negative results and steering: the board-hygiene signals.

These exist because a session analysis found much of the apparent rework was
bookkeeping (in_progress -> todo while waiting on a human) and that valid
negative experiments were being counted as failures.
"""

from tam import digest
from tam.core.workflow import NO_REASON, is_backward
from tam.models import NEGATIVE_LABEL

from .base import TamTestCase


def last_status_note(svc, key):
    events = [e for e in svc.history(key) if e.kind == "transitioned"]
    return events[-1].note


class BackwardMoveReasonTest(TamTestCase):
    def test_backward_move_without_reason_is_stamped(self):
        i = self.mk("Work")
        self.advance_to(i.key, "in_progress")
        self.svc.transition(i.key, "todo")                      # accepted, not rejected
        self.assertEqual(self.svc.get_issue(i.key).status, "todo")
        self.assertEqual(last_status_note(self.svc, i.key), NO_REASON)

    def test_reason_is_kept_when_given(self):
        i = self.mk("Work")
        self.advance_to(i.key, "in_progress")
        self.svc.transition(i.key, "todo", reason="waiting on user review of the pilot")
        self.assertEqual(last_status_note(self.svc, i.key),
                         "waiting on user review of the pilot")

    def test_blank_reason_counts_as_missing(self):
        i = self.mk("Work")
        self.advance_to(i.key, "in_progress")
        self.svc.transition(i.key, "todo", reason="   ")
        self.assertEqual(last_status_note(self.svc, i.key), NO_REASON)

    def test_rejected_from_review_and_reopened_from_done_are_backward(self):
        a = self.mk("A")
        self.advance_to(a.key, "review")
        self.svc.transition(a.key, "in_progress")
        self.assertEqual(last_status_note(self.svc, a.key), NO_REASON)

        b = self.mk("B")
        self.advance_to(b.key, "done")
        self.svc.transition(b.key, "in_progress", reason="regression found")
        self.assertEqual(last_status_note(self.svc, b.key), "regression found")

    def test_forward_moves_are_not_stamped(self):
        i = self.mk("Work")
        self.advance_to(i.key, "review")
        self.svc.transition(i.key, "done")
        notes = [e.note for e in self.svc.history(i.key) if e.kind == "transitioned"]
        self.assertNotIn(NO_REASON, notes)

    def test_unblocking_is_not_a_backward_move(self):
        self.assertFalse(is_backward("blocked", "todo"))
        self.assertFalse(is_backward("todo", "in_progress"))
        self.assertTrue(is_backward("in_progress", "todo"))


class DigestHygieneTest(TamTestCase):
    def test_unexplained_moves_listed_and_explained_ones_are_not(self):
        bare = self.mk("Bare")
        said = self.mk("Said")
        for i in (bare, said):
            self.advance_to(i.key, "in_progress")
        self.svc.transition(bare.key, "todo")
        self.svc.transition(said.key, "todo", reason="needs a decision from the user")

        payload = digest.build(self.svc)
        keys = [m["issue"] for m in payload["moves_back_unexplained"]]

        self.assertEqual(keys, [bare.key])
        self.assertEqual(payload["moves_back_unexplained"][0]["from"], "in_progress")
        self.assertIn("1 move(s) back without a reason", payload["summary"])
        self.assertIn("## Moves back without a reason (1)", digest.render_markdown(payload))

    def test_clean_board_reports_nothing(self):
        i = self.mk("Fine")
        self.advance_to(i.key, "review")
        payload = digest.build(self.svc)
        self.assertEqual(payload["moves_back_unexplained"], [])
        self.assertNotIn("Moves back", digest.render_markdown(payload))

    def test_negative_result_is_marked_in_closed_since_last_digest(self):
        neg = self.mk("Hypothesis tested", labels=[NEGATIVE_LABEL])
        pos = self.mk("Feature shipped")
        self.advance_to(neg.key, "done")
        self.advance_to(pos.key, "done")

        payload = digest.build(self.svc)
        closed = {c["key"]: c for c in payload["sections"]["closed_recently"]}

        self.assertTrue(closed[neg.key]["negative"])
        self.assertFalse(closed[pos.key]["negative"])
        md = digest.render_markdown(payload)
        self.assertIn("[negative result]", md)
        self.assertEqual(md.count("[negative result]"), 1)

    def test_steering_lists_open_issues_over_the_threshold_using_latest_value(self):
        hot = self.mk("Needed lots of prompts")
        cool = self.mk("Easy")
        done = self.mk("Already closed")
        for i in (hot, cool, done):
            self.advance_to(i.key, "in_progress")
        self.svc.record_metric(hot.key, digest.STEERING_METRIC, 3, step=0, source="t",
                               at="2026-01-01T00:00:00Z")
        self.svc.record_metric(hot.key, digest.STEERING_METRIC, 9, step=1, source="t",
                               at="2026-01-02T00:00:00Z")
        self.svc.record_metric(cool.key, digest.STEERING_METRIC, digest.STEERING_MIN - 1,
                               step=0, source="t")
        self.svc.record_metric(done.key, digest.STEERING_METRIC, 20, step=0, source="t")
        self.svc.transition(done.key, "done")

        payload = digest.build(self.svc)

        self.assertEqual([s["key"] for s in payload["steering"]], [hot.key])
        self.assertEqual(payload["steering"][0]["prompts"], 9)
        self.assertIn("## Most steered open issues (1)", digest.render_markdown(payload))

    def test_steering_ignores_containers(self):
        """An epic's window covers all its children, so its count is meaningless."""
        epic = self.mk("Epic")
        child = self.mk("Child", parent=epic.key)
        for i in (epic, child):
            self.advance_to(i.key, "in_progress")
            self.svc.record_metric(i.key, digest.STEERING_METRIC, 40, step=0, source="t")

        payload = digest.build(self.svc)

        self.assertEqual([s["key"] for s in payload["steering"]], [child.key])
