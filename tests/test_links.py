"""Link pairing, cycle detection, and removal."""

from tam.errors import ConflictError, NotFoundError, ValidationError

from .base import TamTestCase


class TestLinks(TamTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.mk("a")
        self.b = self.mk("b")
        self.c = self.mk("c")

    def links_of(self, key):
        return {(l.type, l.to_key) for l in self.svc.list_links(key)}

    def test_blocks_creates_the_inverse(self):
        self.svc.add_link(self.a.key, self.b.key, "blocks")
        self.assertEqual(self.links_of(self.a.key), {("blocks", self.b.key)})
        self.assertEqual(self.links_of(self.b.key), {("blocked_by", self.a.key)})

    def test_relates_to_is_symmetric(self):
        self.svc.add_link(self.a.key, self.b.key, "relates_to")
        self.assertEqual(self.links_of(self.a.key), {("relates_to", self.b.key)})
        self.assertEqual(self.links_of(self.b.key), {("relates_to", self.a.key)})

    def test_duplicates_creates_the_inverse(self):
        self.svc.add_link(self.a.key, self.b.key, "duplicates")
        self.assertEqual(self.links_of(self.b.key), {("duplicated_by", self.a.key)})

    def test_direct_cycle_is_rejected(self):
        self.svc.add_link(self.a.key, self.b.key, "blocks")
        with self.assertRaises(ConflictError):
            self.svc.add_link(self.b.key, self.a.key, "blocks")

    def test_transitive_cycle_is_rejected(self):
        self.svc.add_link(self.a.key, self.b.key, "blocks")
        self.svc.add_link(self.b.key, self.c.key, "blocks")
        with self.assertRaises(ConflictError):
            self.svc.add_link(self.c.key, self.a.key, "blocks")

    def test_cycle_detection_covers_the_inverse_direction(self):
        self.svc.add_link(self.a.key, self.b.key, "blocks")
        with self.assertRaises(ConflictError):
            self.svc.add_link(self.a.key, self.b.key, "blocked_by")

    def test_self_link_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.add_link(self.a.key, self.a.key, "relates_to")

    def test_adding_twice_is_idempotent(self):
        self.svc.add_link(self.a.key, self.b.key, "blocks")
        self.svc.add_link(self.a.key, self.b.key, "blocks")
        self.assertEqual(len(self.svc.list_links(self.a.key)), 1)

    def test_removal_clears_both_directions(self):
        self.svc.add_link(self.a.key, self.b.key, "blocks")
        self.svc.remove_link(self.a.key, self.b.key, "blocks")
        self.assertEqual(self.links_of(self.a.key), set())
        self.assertEqual(self.links_of(self.b.key), set())

    def test_removing_a_missing_link_raises(self):
        with self.assertRaises(NotFoundError):
            self.svc.remove_link(self.a.key, self.b.key, "blocks")

    def test_deleting_an_issue_removes_its_links(self):
        self.svc.add_link(self.a.key, self.b.key, "blocks")
        self.svc.delete_issue(self.a.key)
        self.assertEqual(self.links_of(self.b.key), set())

    def test_blockers_ignores_closed_blockers(self):
        self.svc.add_link(self.a.key, self.b.key, "blocked_by")
        self.assertEqual(len(self.svc.blockers(self.a.key)), 1)
        self.advance_to(self.b.key, "done")
        self.assertEqual(len(self.svc.blockers(self.a.key)), 0)
        self.assertEqual(len(self.svc.blockers(self.a.key, open_only=False)), 1)
