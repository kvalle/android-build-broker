from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import broker
from tests.support import repository


class InitTests(unittest.TestCase):
    def test_init_installs_client_and_respects_equivalent_ignore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = repository(Path(temporary) / "repo")
            args = argparse.Namespace(repository=str(repo))
            with mock.patch("builtins.input", return_value="y"):
                self.assertEqual(0, broker.init_repo(args))
            client = repo / "scripts" / "request-android-build.py"
            self.assertTrue(client.is_file())
            self.assertTrue(client.stat().st_mode & 0o111)
            self.assertEqual(1, (repo / ".gitignore").read_text().count(".artifacts/"))

    def test_init_refuses_unknown_client_and_decline_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = repository(Path(temporary) / "repo")
            scripts = repo / "scripts"; scripts.mkdir()
            client = scripts / "request-android-build.py"
            client.write_text("unknown\n")
            with self.assertRaises(ValueError):
                broker.init_repo(argparse.Namespace(repository=str(repo)))
            client.unlink()
            with mock.patch("builtins.input", return_value="n"):
                self.assertEqual(1, broker.init_repo(argparse.Namespace(repository=str(repo))))
            self.assertFalse(client.exists())

    def test_init_refuses_symlinked_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = repository(root / "repo")
            outside = root / "outside"; outside.mkdir()
            (repo / "scripts").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                broker.init_repo(argparse.Namespace(repository=str(repo)))
            (repo / "scripts").unlink()
            (repo / ".gitignore").unlink()
            (repo / ".gitignore").symlink_to(outside / "ignore")
            with self.assertRaises(ValueError):
                broker.init_repo(argparse.Namespace(repository=str(repo)))


if __name__ == "__main__":
    unittest.main()
