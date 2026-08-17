"""The pull queue: ordering, blocker exclusion, WIP headroom, starved lanes."""

from tam.core import queue

from .base import TamTestCase


class TestQueue(TamTestCase):
    def setUp(self):
        super().setUp()
        self.epic = self.mk("Epic")

    def todo(self, title, **kw):
        i = self.mk(title, parent=self.epic.key, **kw)
        self.svc.transition(i.key, "todo")
        return i

    def working(self, title):
        i = self.mk(title, parent=self.epic.key)
        self.svc.transition(i.key, "todo")
        self.svc.transition(i.key, "in_progress")
        return i

    def keys(self, rows):
        return [r["key"] for r in rows]

    def close(self, key):
        """Walk the legal path to done -- backlog cannot close directly."""
        for status in ("todo", "in_progress", "done"):
            self.svc.transition(key, status)

    # -- membership --------------------------------------------------------

    def test_only_todo_is_queued(self):
        self.todo("queued")
        self.mk("still in backlog", parent=self.epic.key)
        self.working("already started")
        pull = self.svc.next_up()
        self.assertEqual([r["title"] for r in pull["ready"]], ["queued"])

    def test_an_open_blocker_moves_it_out_of_ready(self):
        blocked = self.todo("needs the other thing")
        blocker = self.mk("the other thing")
        self.svc.add_link(blocker.key, blocked.key, "blocks")

        pull = self.svc.next_up()
        self.assertEqual(self.keys(pull["ready"]), [])
        self.assertEqual(self.keys(pull["blocked"]), [blocked.key])
        self.assertEqual(pull["blocked"][0]["blocked_by"][0]["key"], blocker.key)

    def test_closing_the_blocker_returns_it_to_ready(self):
        blocked = self.todo("needs the other thing")
        blocker = self.mk("the other thing")
        self.svc.add_link(blocker.key, blocked.key, "blocks")
        self.close(blocker.key)

        pull = self.svc.next_up()
        self.assertEqual(self.keys(pull["ready"]), [blocked.key])
        self.assertEqual(pull["blocked"], [])

    def test_a_container_is_never_queued(self):
        """An epic is a lane header, not work. It must not appear as pullable."""
        parent = self.mk("parent")
        self.svc.transition(parent.key, "todo")
        self.mk("child", parent=parent.key)
        pull = self.svc.next_up()
        self.assertNotIn(parent.key, self.keys(pull["ready"]))

    # -- ordering ----------------------------------------------------------

    def test_priority_then_due_date_then_age(self):
        low = self.todo("low", priority="p3")
        high = self.todo("high", priority="p0")
        mid_late = self.todo("mid, due later", priority="p2", due_date="2030-01-02")
        mid_soon = self.todo("mid, due sooner", priority="p2", due_date="2030-01-01")
        mid_undated = self.todo("mid, undated", priority="p2")

        order = self.keys(self.svc.next_up(wip_limit=99)["ready"])
        self.assertEqual(
            order, [high.key, mid_soon.key, mid_late.key, mid_undated.key, low.key])

    def test_dating_an_issue_pulls_it_ahead_of_undated_peers(self):
        old_undated = self.todo("older but undated")
        dated = self.todo("newer with a date", due_date="2030-01-01")
        order = self.keys(self.svc.next_up(wip_limit=99)["ready"])
        self.assertEqual(order, [dated.key, old_undated.key])

    # -- headroom ----------------------------------------------------------

    def test_pullable_is_capped_by_the_lane_wip_limit(self):
        self.working("running one")
        a = self.todo("first", priority="p0")
        b = self.todo("second", priority="p1")
        c = self.todo("third", priority="p2")

        pull = self.svc.next_up(wip_limit=3)
        self.assertEqual([r["key"] for r in pull["ready"] if r["pullable"]],
                         [a.key, b.key])
        self.assertEqual([r["key"] for r in pull["ready"] if not r["pullable"]],
                         [c.key])
        self.assertEqual(pull["pullable_now"], 2)

    def test_a_full_lane_queues_everything(self):
        for n in range(3):
            self.working(f"running {n}")
        self.todo("waiting")
        pull = self.svc.next_up(wip_limit=3)
        self.assertEqual(pull["pullable_now"], 0)
        self.assertFalse(pull["ready"][0]["pullable"])

    def test_headroom_ignores_the_container(self):
        """The epic sits in_progress once a child does; it must not eat a slot."""
        self.working("running one")
        self.todo("waiting")
        pull = self.svc.next_up(wip_limit=2)
        lane = next(l for l in pull["lanes"] if l["lane"] == self.epic.key)
        self.assertEqual(lane["in_progress"], 1)
        self.assertEqual(lane["headroom"], 1)

    def test_lanes_are_independent(self):
        other = self.mk("Other epic")
        for n in range(3):
            self.working(f"busy {n}")
        free = self.mk("free lane work", parent=other.key)
        self.svc.transition(free.key, "todo")

        pull = self.svc.next_up(wip_limit=3)
        self.assertEqual([r["key"] for r in pull["ready"] if r["pullable"]],
                         [free.key])

    # -- starved lanes -----------------------------------------------------

    def test_room_but_nothing_queued_names_the_backlog_candidate(self):
        self.working("running one")
        low = self.mk("backlog low", parent=self.epic.key, priority="p3")
        top = self.mk("backlog top", parent=self.epic.key, priority="p1")

        pull = self.svc.next_up(wip_limit=3)
        self.assertEqual(pull["ready"], [])
        starved = next(s for s in pull["starved"] if s["lane"] == self.epic.key)
        self.assertEqual(starved["headroom"], 2)
        self.assertEqual(starved["backlog_count"], 2)
        self.assertEqual(starved["candidate"]["key"], top.key)
        self.assertNotEqual(starved["candidate"]["key"], low.key)

    def test_a_lane_with_ready_work_is_not_starved(self):
        self.working("running one")
        self.todo("ready")
        pull = self.svc.next_up(wip_limit=3)
        self.assertEqual([s["lane"] for s in pull["starved"]], [])

    def test_a_full_lane_is_not_starved(self):
        for n in range(3):
            self.working(f"running {n}")
        pull = self.svc.next_up(wip_limit=3)
        self.assertEqual([s["lane"] for s in pull["starved"]], [])

    def test_starved_reports_an_empty_backlog_as_no_candidate(self):
        self.working("running one")
        pull = self.svc.next_up(wip_limit=3)
        starved = next(s for s in pull["starved"] if s["lane"] == self.epic.key)
        self.assertIsNone(starved["candidate"])
        self.assertEqual(starved["backlog_count"], 0)

    # -- parentless work ---------------------------------------------------

    def test_issues_with_no_parent_queue_under_the_sentinel(self):
        loose = self.mk("standalone")
        self.svc.transition(loose.key, "todo")
        pull = self.svc.next_up()
        row = next(r for r in pull["ready"] if r["key"] == loose.key)
        self.assertEqual(row["lane"], queue.NO_EPIC)

    def test_empty_board_is_not_an_error(self):
        pull = self.svc.next_up()
        self.assertEqual(pull["ready"], [])
        self.assertEqual(pull["blocked"], [])
        self.assertEqual(pull["pullable_now"], 0)


class TestQueueInDigest(TamTestCase):
    def test_digest_carries_the_queue(self):
        epic = self.mk("Epic")
        ready = self.mk("ready thing", parent=epic.key, priority="p1")
        self.svc.transition(ready.key, "todo")

        payload = self.svc.digest()
        self.assertIn("next_up", payload["sections"])
        self.assertEqual([i["key"] for i in payload["sections"]["next_up"]],
                         [ready.key])
        self.assertEqual(payload["pull"]["pullable_now"], 1)

    def test_next_up_renders_with_a_start_now_marker(self):
        epic = self.mk("Epic")
        ready = self.mk("ready thing", parent=epic.key)
        self.svc.transition(ready.key, "todo")

        from tam import digest as digest_mod
        text = digest_mod.render_markdown(self.svc.digest())
        self.assertIn("Next up", text)
        self.assertIn("1 can start now", text)
        self.assertIn(f"->{ready.key}", text.replace(" ", ""))
