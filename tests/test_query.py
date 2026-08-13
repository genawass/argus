"""The filter/sort engine."""

from tam.clock import days_ago
from tam.core import IssueFilter
from tam.errors import ValidationError

from .base import TamTestCase


class TestFilters(TamTestCase):
    def keys(self, **kw):
        return [i.key for i in self.svc.list_issues(IssueFilter(**kw))]

    def test_closed_issues_are_excluded_by_default(self):
        a = self.mk("open")
        b = self.mk("closed")
        self.advance_to(b.key, "done")
        self.assertEqual(self.keys(), [a.key])
        self.assertEqual(sorted(self.keys(include_closed=True)), [a.key, b.key])

    def test_explicit_status_filter_overrides_the_default(self):
        b = self.mk("closed")
        self.advance_to(b.key, "done")
        self.assertEqual(self.keys(status=("done",)), [b.key])

    def test_priority_and_type_filters(self):
        a = self.mk("a", priority="p0", type="bug")
        self.mk("b", priority="p3", type="task")
        self.assertEqual(self.keys(priority=("p0",)), [a.key])
        self.assertEqual(self.keys(type=("bug",)), [a.key])

    def test_labels_are_anded(self):
        a = self.mk("a", labels=["backend", "auth"])
        self.mk("b", labels=["backend"])
        self.assertEqual(self.keys(labels=("backend", "auth")), [a.key])
        self.assertEqual(len(self.keys(labels=("backend",))), 2)

    def test_text_search_covers_title_and_body(self):
        a = self.mk("Renew TLS certificates")
        b = self.mk("Other", body="mentions certificates too")
        self.assertEqual(sorted(self.keys(text="certificates")), sorted([a.key, b.key]))
        self.assertEqual(self.keys(text="Renew"), [a.key])

    def test_text_search_survives_punctuation(self):
        a = self.mk('Fix the "quoted" thing (urgent)')
        self.assertEqual(self.keys(text='"quoted"'), [a.key])
        self.assertEqual(self.keys(text="nonexistent"), [])

    def test_overdue_uses_local_today_and_skips_closed(self):
        overdue = self.mk("late", due_date="2000-01-01")
        future = self.mk("soon", due_date="2999-01-01")
        closed = self.mk("late but done", due_date="2000-01-01")
        self.advance_to(closed.key, "done")
        self.assertEqual(self.keys(overdue=True), [overdue.key])
        self.assertNotIn(future.key, self.keys(overdue=True))

    def test_due_range_filters(self):
        a = self.mk("a", due_date="2026-01-10")
        b = self.mk("b", due_date="2026-01-20")
        self.mk("undated")
        self.assertEqual(self.keys(due_before="2026-01-15"), [a.key])
        self.assertEqual(self.keys(due_after="2026-01-15"), [b.key])
        self.assertEqual(self.keys(due_on="2026-01-20"), [b.key])

    def test_stale_days(self):
        fresh = self.mk("fresh")
        stale = self.mk("stale")
        self.backdate(stale.key, days_ago("UTC", 30))
        self.assertEqual(self.keys(stale_days=7), [stale.key])
        self.assertIn(fresh.key, self.keys())

    def test_assignee_and_unassigned(self):
        mine = self.mk("mine", assignee="me")
        free = self.mk("free")
        self.assertEqual(self.keys(assignee="me"), [mine.key])
        self.assertEqual(self.keys(assignee="unassigned"), [free.key])

    def test_parent_filters(self):
        parent = self.mk("parent")
        child = self.mk("child", parent=parent.key)
        self.assertEqual(self.keys(parent=parent.key), [child.key])
        self.assertEqual(self.keys(has_parent=True), [child.key])
        self.assertEqual(self.keys(has_parent=False), [parent.key])

    def test_is_blocked_covers_status_and_open_blockers(self):
        by_status = self.mk("stuck", status="todo")
        self.svc.transition(by_status.key, "blocked", reason="waiting")
        by_link = self.mk("waiting")
        blocker = self.mk("blocker")
        self.svc.add_link(by_link.key, blocker.key, "blocked_by")
        self.assertEqual(sorted(self.keys(is_blocked=True)),
                         sorted([by_status.key, by_link.key]))
        self.advance_to(blocker.key, "done")
        self.assertEqual(self.keys(is_blocked=True), [by_status.key])

    def test_project_filter(self):
        self.svc.create_project("OPS", "Ops")
        self.mk("tam one")
        ops = self.mk("ops one", project="OPS")
        self.assertEqual(self.keys(project="OPS"), [ops.key])


class TestSorting(TamTestCase):
    def keys(self, **kw):
        return [i.key for i in self.svc.list_issues(IssueFilter(**kw))]

    def test_priority_sort_puts_p0_first(self):
        low = self.mk("low", priority="p3")
        high = self.mk("high", priority="p0")
        self.assertEqual(self.keys(sort="priority"), [high.key, low.key])
        self.assertEqual(self.keys(sort="priority", order="desc"), [low.key, high.key])

    def test_undated_issues_sort_last_in_both_directions(self):
        dated = self.mk("dated", due_date="2026-01-01")
        undated = self.mk("undated")
        self.assertEqual(self.keys(sort="due_date"), [dated.key, undated.key])
        self.assertEqual(self.keys(sort="due_date", order="desc"), [dated.key, undated.key])

    def test_key_sort_is_numeric_not_lexicographic(self):
        for i in range(11):
            self.mk(f"t{i}")
        keys = self.keys(sort="key")
        self.assertEqual(keys[:3], ["TAM-1", "TAM-2", "TAM-3"])
        self.assertEqual(keys[-1], "TAM-11")

    def test_limit_and_offset(self):
        for i in range(5):
            self.mk(f"t{i}")
        self.assertEqual(len(self.keys(limit=2)), 2)
        self.assertEqual(self.keys(sort="key", limit=2, offset=2), ["TAM-3", "TAM-4"])

    def test_bad_sort_and_order_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.list_issues(IssueFilter(sort="nonsense"))
        with self.assertRaises(ValidationError):
            self.svc.list_issues(IssueFilter(order="sideways"))


class TestStats(TamTestCase):
    def test_counts_split_open_and_closed(self):
        self.mk("a", priority="p0")
        b = self.mk("b")
        self.advance_to(b.key, "done")
        s = self.svc.stats()
        self.assertEqual((s["total"], s["open"], s["closed"]), (2, 1, 1))
        self.assertEqual(s["by_status"]["done"], 1)
        self.assertEqual(s["open_by_priority"]["p0"], 1)

    def test_stats_can_scope_to_a_project(self):
        self.svc.create_project("OPS", "Ops")
        self.mk("tam")
        self.mk("ops", project="OPS")
        self.assertEqual(self.svc.stats(project="OPS")["total"], 1)
