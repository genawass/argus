"""Digest assembly, and what the WIP number is allowed to count."""

from tam import digest

from .base import TamTestCase


class DigestWipTest(TamTestCase):
    def test_wip_counts_leaf_issues_only(self):
        """A container is in_progress because its children are, not because
        anyone is working on it, so it must not consume WIP."""
        epic = self.mk("Epic")
        child = self.mk("Child", parent=epic.key)
        solo = self.mk("Standalone")
        for key in (epic.key, child.key, solo.key):
            self.advance_to(key, "in_progress")

        payload = digest.build(self.svc)

        self.assertEqual(len(payload["sections"]["in_progress"]), 3)
        self.assertEqual(payload["wip"]["count"], 2)

    def test_container_with_only_closed_children_still_excluded(self):
        """Closing the children does not turn a container back into work."""
        epic = self.mk("Epic")
        child = self.mk("Child", parent=epic.key)
        self.advance_to(epic.key, "in_progress")
        self.advance_to(child.key, "done")

        payload = digest.build(self.svc)

        self.assertEqual(payload["wip"]["count"], 0)

    def test_wip_over_limit_reflects_leaf_count(self):
        """The `over` flag follows the corrected count, not the raw one."""
        epic = self.mk("Epic")
        self.advance_to(epic.key, "in_progress")
        for n in range(self.config.wip_limit):
            child = self.mk(f"Child {n}", parent=epic.key)
            self.advance_to(child.key, "in_progress")

        payload = digest.build(self.svc)

        self.assertEqual(payload["wip"]["count"], self.config.wip_limit)
        self.assertFalse(payload["wip"]["over"])
