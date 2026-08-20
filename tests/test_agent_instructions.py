import tempfile
from pathlib import Path

from tests.base import TamTestCase
from tools.install_agent_instructions import BEGIN, END, _block, update


class TestAgentInstructions(TamTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tam-agent-instructions-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = Path(__file__).resolve().parents[1]

    def test_install_is_idempotent(self):
        path = self.home / ".codex" / "AGENTS.md"
        block = _block("codex", self.root)
        self.assertEqual(update(path, block), "created")
        self.assertEqual(update(path, block), "unchanged")
        text = path.read_text()
        self.assertEqual(text.count(BEGIN), 1)
        self.assertEqual(text.count(END), 1)

    def test_existing_instructions_are_preserved_and_remove_is_safe(self):
        path = self.home / ".claude" / "CLAUDE.md"
        path.parent.mkdir(parents=True)
        path.write_text("# My instructions\n\nKeep this.\n")
        update(path, _block("claude", self.root))
        self.assertIn("Keep this.", path.read_text())
        self.assertEqual(update(path, None), "updated")
        self.assertEqual(path.read_text(), "# My instructions\n\nKeep this.\n")

    def test_remove_deletes_file_when_only_managed_content_remains(self):
        path = self.home / ".gemini" / "GEMINI.md"
        update(path, _block("gemini", self.root))
        self.assertEqual(update(path, None), "removed")
        self.assertFalse(path.exists())

    def test_unmatched_marker_is_refused(self):
        path = self.home / ".codex" / "AGENTS.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"{BEGIN}\nbroken\n")
        with self.assertRaises(ValueError):
            update(path, _block("codex", self.root))
