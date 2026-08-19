from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import worker
from tests.support import read_json, repository, request_file


class WorkerTests(unittest.TestCase):
    def run_worker(self, repo: Path, script_text: str, *, timeout: float = 5) -> tuple[int, dict, Path]:
        session = str(uuid.uuid4())
        request_id, request, result = request_file(repo, session)
        script = repo / "build.sh"
        script.write_text("#!/bin/sh\n" + script_text, encoding="utf-8")
        script.chmod(0o755)
        # The script is intentionally untracked startup configuration, so make
        # the request identity include it after creation.
        identity = worker.compute_source_identity(repo)
        payload = read_json(request)
        payload["head"], payload["worktreeDigest"] = identity.head, identity.digest
        worker.atomic_write_json(request, payload)
        args = argparse.Namespace(repository=str(repo), request=str(request), request_id=request_id, session_id=session,
                                  build_script="build.sh", build_arg=[], artifact=".artifacts/android/output.apk", timeout=timeout)
        code = worker.run(args)
        return code, read_json(result / "status.json"), result

    def test_success_copies_and_hashes_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, status, result = self.run_worker(repository(Path(temporary) / "repo"), "mkdir -p .artifacts/android\nprintf apk > .artifacts/android/output.apk\n")
            self.assertEqual(0, code)
            self.assertEqual("passed", status["state"])
            self.assertEqual(3, status["artifactSize"])
            self.assertEqual(b"apk", (result / "app-smoke.apk").read_bytes())
            self.assertTrue((result / "build.log").is_file())

    def test_nonzero_missing_symlink_and_timeout_fail(self) -> None:
        cases = [
            ("exit 7\n", "build_failed", 5),
            ("exit 0\n", "artifact_missing", 5),
            ("mkdir -p .artifacts/android\nln -s source.txt .artifacts/android/output.apk\n", "artifact_invalid", 5),
            ("sleep 10\n", "build_timeout", 0.05),
        ]
        for script, error, timeout in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary:
                code, status, _result = self.run_worker(repository(Path(temporary) / "repo"), script, timeout=timeout)
                self.assertEqual(0, code)
                self.assertEqual(error, status["errorCode"])

    def test_post_build_source_mutation_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, status, result = self.run_worker(repository(Path(temporary) / "repo"), "mkdir -p .artifacts/android\nprintf apk > .artifacts/android/output.apk\nprintf changed > source.txt\n")
            self.assertEqual(0, code)
            self.assertEqual("stale", status["state"])
            self.assertEqual("source_changed", status["errorCode"])
            self.assertFalse((result / "app-smoke.apk").exists())

    def test_secret_named_environment_values_are_not_exposed_to_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"BROKER_TEST_SECRET_TOKEN": "do-not-log"}):
            code, status, result = self.run_worker(repository(Path(temporary) / "repo"), "mkdir -p .artifacts/android\nenv > .artifacts/android/output.apk\n")
            self.assertEqual(0, code)
            self.assertNotIn(b"do-not-log", (result / "app-smoke.apk").read_bytes())

    def test_build_log_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = repository(Path(temporary) / "repo")
            session = str(uuid.uuid4())
            request_id, request, result = request_file(repo, session)
            outside = Path(temporary) / "outside"; outside.write_text("safe")
            (result / "build.log").symlink_to(outside)
            script = repo / "build.sh"; script.write_text("#!/bin/sh\nexit 0\n"); script.chmod(0o755)
            identity = worker.compute_source_identity(repo)
            payload = read_json(request); payload["head"], payload["worktreeDigest"] = identity.head, identity.digest
            worker.atomic_write_json(request, payload)
            args = argparse.Namespace(repository=str(repo), request=str(request), request_id=request_id, session_id=session,
                                      build_script="build.sh", build_arg=[], artifact=".artifacts/android/output.apk", timeout=5)
            self.assertEqual(0, worker.run(args))
            self.assertEqual("safe", outside.read_text())


if __name__ == "__main__":
    unittest.main()
