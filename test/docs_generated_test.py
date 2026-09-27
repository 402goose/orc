"""Keep the committed references synchronized with the callable interfaces."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gen_docs  # noqa: E402


class GeneratedDocsTest(unittest.TestCase):
    def test_committed_references_are_current(self):
        self.assertEqual(gen_docs.sync_docs(ROOT / "docs", check=True), [],
                         "Run python3 scripts/gen_docs.py and commit both references")

    def test_check_fails_for_missing_or_stale_files_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(gen_docs, "ROOT", root), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gen_docs.main(["--check"]), 1)
                self.assertFalse((root / "docs").exists())
                self.assertEqual(gen_docs.main([]), 0)
                self.assertEqual(gen_docs.main(["--check"]), 0)
                for name in ("cli.md", "mcp.md"):
                    path = root / "docs" / name
                    original = path.read_text(encoding="utf-8")
                    path.write_text("stale\n", encoding="utf-8")
                    self.assertEqual(gen_docs.main(["--check"]), 1)
                    self.assertEqual(path.read_text(encoding="utf-8"), "stale\n")
                    path.write_text(original, encoding="utf-8")

    def test_alias_hidden_and_nested_commands_are_included(self):
        document = gen_docs.cli_document()
        for command in ("runs", "mcp-serve", "workflow publish", "learn schedule", "gym priors"):
            self.assertIn(f"\n## fusion {command}\n", document)


if __name__ == "__main__":
    unittest.main()
