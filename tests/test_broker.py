from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import broker
from protocol import atomic_write_json
from tests.support import read_json, repository


class BrokerTests(unittest.TestCase):
    def queued_request(self, repo: Path, session: str, request_id: str | None = None) -> tuple[Path, str]:
        request_id = request_id or str(uuid.uuid4())
        from source_identity import compute_source_identity
        source = compute_source_identity(repo)
        requests = repo / ".artifacts" / "android" / "requests"
        requests.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = requests / f"{request_id}.json"
        atomic_write_json(path, {"protocolVersion": 1, "requestId": request_id, "createdAt": int(time.time()), "brokerSessionId": session,
                                 "head": source.head, "worktreeDigest": source.digest})
        return path, request_id

    def test_atomic_claim_busy_and_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = repository(Path(temporary) / "repo")
            root, requests, claimed, results = broker.runtime_paths(repo)
            claimed.mkdir(parents=True); results.mkdir(parents=True)
            session = str(uuid.uuid4())
            path, request_id = self.queued_request(repo, session)
            outcome = broker.classify_and_claim(path, claimed, results, session, busy=False)
            self.assertIsNotNone(outcome)
            self.assertFalse(path.exists())
            busy_path, busy_id = self.queued_request(repo, session)
            broker.classify_and_claim(busy_path, claimed, results, session, busy=True)
            self.assertEqual("busy", read_json(results / busy_id / "status.json")["errorCode"])
            old_path, old_id = self.queued_request(repo, str(uuid.uuid4()))
            broker.classify_and_claim(old_path, claimed, results, session, busy=True)
            self.assertEqual("broker_restarted", read_json(results / old_id / "status.json")["errorCode"])

    def test_lock_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(broker, "BROKER_ROOT", Path(temporary)):
            repo = Path(temporary) / "repo"; repo.mkdir()
            first = broker.acquire_lock(repo, str(uuid.uuid4()))
            try:
                with self.assertRaises(RuntimeError):
                    broker.acquire_lock(repo, str(uuid.uuid4()))
            finally:
                first.close()

    def test_retention_deletes_oldest_terminal_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            results = Path(temporary)
            now = 2_000_000_000
            for index in range(23):
                request_id = str(uuid.uuid4())
                (results / request_id).mkdir()
                atomic_write_json(results / request_id / "status.json", {"state": "passed", "updatedAt": now - index})
            old = results / str(uuid.uuid4())
            old.mkdir()
            atomic_write_json(old / "status.json", {"state": "failed", "updatedAt": now - broker.RETENTION_SECONDS - 1})
            active = results / str(uuid.uuid4())
            active.mkdir()
            atomic_write_json(active / "status.json", {"state": "building", "updatedAt": 1})
            broker.retain_results(results, now)
            terminals = [path for path in results.iterdir() if path != active]
            self.assertEqual(20, len(terminals))
            self.assertTrue(active.exists())

    def test_process_group_termination_reaches_child(self) -> None:
        process = subprocess.Popen(
            ["python3", "-c", "import subprocess,time; subprocess.Popen(['sleep','30']); time.sleep(30)"],
            start_new_session=True,
        )
        broker.terminate_process_group(process, grace=0.2)
        self.assertIsNotNone(process.returncode)

    def test_cplt_command_places_flags_before_exec_and_preserves_arguments(self) -> None:
        command = broker.cplt_command(Path("/repo"), Path("/repo/request.json"), str(uuid.uuid4()), str(uuid.uuid4()), "build", ["--flag", "two words"], "out.apk")
        exec_index = command.index("exec")
        self.assertLess(command.index("--allow-localhost-any"), exec_index)
        self.assertIn("--build-arg=--flag", command)
        self.assertIn("--build-arg=two words", command)

    def test_handled_worker_failure_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"; repo.mkdir()
            runtime, requests, claimed, results = broker.runtime_paths(repo)
            for directory in (requests, claimed, results):
                directory.mkdir(parents=True, exist_ok=True)
            request_id = str(uuid.uuid4())
            (results / request_id).mkdir()
            atomic_write_json(results / request_id / "status.json", {
                "protocolVersion": 1, "requestId": request_id, "state": "failed",
                "errorCode": "build_failed", "updatedAt": int(time.time()),
            })
            command = ["python3", "-c", "raise SystemExit(0)"]
            broker.run_build(command, runtime, repo, requests, claimed, results, str(uuid.uuid4()), request_id)
            self.assertEqual("build_failed", read_json(results / request_id / "status.json")["errorCode"])


if __name__ == "__main__":
    unittest.main()
