from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from broker import generated_client
from source_identity import SourceIdentityError, compute_source_identity
from tests.support import git, repository


class SourceIdentityTests(unittest.TestCase):
    def test_identity_tracks_worktree_inputs_but_not_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = repository(Path(temporary) / "repo")
            initial = compute_source_identity(repo)
            (repo / "ignored").mkdir()
            (repo / "ignored" / "output").write_text("ignored")
            self.assertEqual(initial, compute_source_identity(repo))
            (repo / "source.txt").write_text("modified")
            modified = compute_source_identity(repo)
            self.assertNotEqual(initial.digest, modified.digest)
            (repo / "source.txt").unlink()
            deleted = compute_source_identity(repo)
            self.assertNotEqual(modified.digest, deleted.digest)
            (repo / "source.txt").write_text("source\n")
            os.chmod(repo / "source.txt", 0o755)
            executable = compute_source_identity(repo)
            self.assertNotEqual(initial.digest, executable.digest)
            odd = repo / "line\nbreak-cafe\u0301"
            odd.write_bytes(b"odd")
            self.assertNotEqual(executable.digest, compute_source_identity(repo).digest)

    def test_generated_client_uses_identical_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = repository(root / "repo")
            client = root / "client.py"
            client.write_text(generated_client(), encoding="utf-8")
            output = subprocess.check_output([
                "python3", "-c",
                "import runpy,sys; m=runpy.run_path(sys.argv[1]); i=m['compute_source_identity'](sys.argv[2]); print(i.head, i.digest)",
                client, repo,
            ], text=True).strip()
            identity = compute_source_identity(repo)
            self.assertEqual(f"{identity.head} {identity.digest}", output)


if __name__ == "__main__":
    unittest.main()
