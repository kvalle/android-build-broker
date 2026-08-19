#!/usr/bin/env python3
"""Build worker executed inside a fresh cplt sandbox."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import re
from pathlib import Path

from protocol import ProtocolError, atomic_write_json, open_regular_nofollow, read_secure_request, secure_runtime_directory, status_payload
from source_identity import SourceIdentityError, compute_source_identity

BUILD_TIMEOUT_SECONDS = 30 * 60
SECRET_ENVIRONMENT_NAME = re.compile(r"(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|PRIVATE_KEY|API_KEY|AUTH)", re.IGNORECASE)


def terminal(status_path: Path, request_id: str, state: str, error_code: str | None = None, **fields: object) -> int:
    if error_code:
        fields["errorCode"] = error_code
    atomic_write_json(status_path, status_payload(request_id, state, **fields))
    return 0 if state == "passed" else 1


def repository_path(repository: Path, relative: str, *, executable: bool = False) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"Path must be repository-relative: {relative}")
    full = repository / candidate
    current = repository
    for component in candidate.parts:
        current /= component
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Path contains a symlink: {relative}")
    if full.resolve(strict=False) != repository / candidate:
        raise ValueError(f"Path escapes repository: {relative}")
    if executable:
        info = full.lstat()
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            raise ValueError(f"Build script must be an executable ordinary file: {relative}")
    return full


def terminate_process_group(process: subprocess.Popen[bytes], grace: float = 5.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def terminate_remaining_group(group_id: int, grace: float = 0.2) -> None:
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(grace)
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run(args: argparse.Namespace) -> int:
    repository = Path(args.repository).resolve(strict=True)
    claimed = Path(args.request).absolute()
    result_dir = repository / ".artifacts" / "android" / "results" / args.request_id
    try:
        secure_runtime_directory(result_dir)
    except ProtocolError as exc:
        print(f"worker: unsafe result directory: {exc}", file=sys.stderr)
        return 1
    status_path = result_dir / "status.json"
    log_path = result_dir / "build.log"
    try:
        request = read_secure_request(claimed)
        if request.request_id != args.request_id:
            raise ProtocolError("invalid_request", "Claimed request ID differs from command")
        if request.broker_session_id != args.session_id:
            return terminal(status_path, args.request_id, "rejected", "broker_restarted")
        build_script = repository_path(repository, args.build_script, executable=True)
        try:
            artifact = repository_path(repository, args.artifact)
        except ValueError:
            return terminal(status_path, args.request_id, "failed", "artifact_invalid")
        before = compute_source_identity(repository)
    except (ProtocolError, SourceIdentityError, ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, ProtocolError) else "validation_failed"
        return terminal(status_path, args.request_id, "failed", code, message=str(exc))
    if before.head != request.head or before.digest != request.worktree_digest:
        return terminal(status_path, args.request_id, "stale", "source_mismatch")
    atomic_write_json(status_path, status_payload(args.request_id, "building"))
    try:
        if artifact.exists() or artifact.is_symlink():
            info = artifact.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return terminal(status_path, args.request_id, "failed", "artifact_invalid")
            artifact.unlink()
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_fd = open_regular_nofollow(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with os.fdopen(log_fd, "wb") as log:
            process = subprocess.Popen(
                [os.fspath(build_script), *args.build_arg],
                cwd=repository,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={name: value for name, value in os.environ.items() if not SECRET_ENVIRONMENT_NAME.search(name)},
            )
            try:
                exit_code = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                log.flush()
                os.fsync(log.fileno())
                return terminal(status_path, args.request_id, "failed", "build_timeout")
            terminate_remaining_group(process.pid)
            log.flush()
            os.fsync(log.fileno())
        if exit_code:
            return terminal(status_path, args.request_id, "failed", "build_failed", exitCode=exit_code)
        try:
            artifact = repository_path(repository, args.artifact)
        except ValueError:
            return terminal(status_path, args.request_id, "failed", "artifact_invalid")
        try:
            artifact_info = artifact.lstat()
        except FileNotFoundError:
            return terminal(status_path, args.request_id, "failed", "artifact_missing")
        if not stat.S_ISREG(artifact_info.st_mode) or stat.S_ISLNK(artifact_info.st_mode):
            return terminal(status_path, args.request_id, "failed", "artifact_invalid")
        output_path = result_dir / "app-smoke.apk"
        digest = hashlib.sha256()
        size = 0
        source_fd = os.open(artifact, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        output_fd = open_regular_nofollow(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(source_fd, "rb") as source, os.fdopen(output_fd, "wb") as output:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (artifact_info.st_dev, artifact_info.st_ino):
                raise OSError("Artifact changed while opening")
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = compute_source_identity(repository)
        if after != before or after.head != request.head or after.digest != request.worktree_digest:
            output_path.unlink()
            return terminal(status_path, args.request_id, "stale", "source_changed")
        return terminal(status_path, args.request_id, "passed", artifactPath="app-smoke.apk", artifactSize=size, artifactSha256=digest.hexdigest())
    except (OSError, SourceIdentityError, subprocess.SubprocessError) as exc:
        try:
            (result_dir / "app-smoke.apk").unlink()
        except FileNotFoundError:
            pass
        return terminal(status_path, args.request_id, "failed", "artifact_failed", message=str(exc))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--repository", required=True)
    value.add_argument("--request", required=True)
    value.add_argument("--request-id", required=True)
    value.add_argument("--session-id", required=True)
    value.add_argument("--build-script", required=True)
    value.add_argument("--build-arg", action="append", default=[])
    value.add_argument("--artifact", required=True)
    value.add_argument("--timeout", type=float, default=BUILD_TIMEOUT_SECONDS)
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
