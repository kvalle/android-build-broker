from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from protocol import atomic_write_json
from source_identity import compute_source_identity


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", repository, *arguments], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / ".gitignore").write_text(".artifacts/\nignored/\n", encoding="utf-8")
    (path / "source.txt").write_text("source\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "initial")
    return path


def request_file(repository: Path, session: str, *, request_id: str | None = None, mode: int = 0o600) -> tuple[str, Path, Path]:
    request_id = request_id or str(uuid.uuid4())
    identity = compute_source_identity(repository)
    root = repository / ".artifacts" / "android"
    claimed = root / "claimed"
    result = root / "results" / request_id
    claimed.mkdir(parents=True, mode=0o700)
    result.mkdir(parents=True, mode=0o700)
    path = claimed / f"{request_id}.json"
    atomic_write_json(path, {
        "protocolVersion": 1,
        "requestId": request_id,
        "createdAt": int(time.time()),
        "brokerSessionId": session,
        "head": identity.head,
        "worktreeDigest": identity.digest,
    }, mode=mode)
    return request_id, path, result


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
