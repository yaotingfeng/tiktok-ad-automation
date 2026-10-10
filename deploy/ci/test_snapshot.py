"""Credential isolation regression for the local full-check source snapshot."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from snapshot import copy_sources


class SnapshotTests(unittest.TestCase):
    def test_copies_source_changes_but_never_private_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "repo"
            target = Path(temporary) / "snapshot"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            (source / ".gitignore").write_text(".runtime/\nprivate.json\n")
            for name in ("tracked.py", "deleted.py", ".env", ".env.production", ".env.example"):
                (source / name).write_text("fixture")
            subprocess.run(
                ["git", "add", ".gitignore", "tracked.py", "deleted.py", ".env", ".env.production", ".env.example"],
                cwd=source,
                check=True,
            )
            (source / "tracked.py").write_text("reviewed change")
            (source / "deleted.py").unlink()
            (source / "new.py").write_text("new source")
            (source / "private.json").write_text("private fixture")
            (source / ".runtime").mkdir()
            (source / ".runtime" / "credential").write_text("private fixture")
            copy_sources(source, target)
            self.assertEqual((target / "tracked.py").read_text(), "reviewed change")
            self.assertTrue((target / "new.py").exists())
            self.assertTrue((target / ".env.example").exists())
            for name in ("deleted.py", ".env", ".env.production", "private.json", ".runtime"):
                self.assertFalse((target / name).exists(), name)

    def test_rejects_symlinks_outside_source_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "repo"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            (source / "leak").symlink_to(Path(temporary) / "private")
            with self.assertRaisesRegex(RuntimeError, "escapes repository"):
                copy_sources(source, Path(temporary) / "snapshot")


if __name__ == "__main__":
    unittest.main()
