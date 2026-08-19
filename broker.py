#!/usr/bin/env python3
"""Foreground host broker for Android smoke APK requests."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from protocol import PROTOCOL_VERSION, TERMINAL_STATES, ProtocolError, atomic_write_json, read_secure_request, secure_runtime_directory, status_payload
from worker import repository_path, terminate_process_group

BROKER_ROOT = Path(__file__).resolve().parent
TRUSTED_DEFAULT_REPOSITORY_NAME = "trene"
RETENTION_SECONDS = 7 * 24 * 60 * 60
RETENTION_COUNT = 20


class Stopping:
    requested = False


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def canonical_git_repository(value: str) -> Path:
    repository = Path(value).expanduser().resolve(strict=True)
    result = subprocess.run(["git", "-C", repository, "rev-parse", "--show-toplevel"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode or Path(result.stdout.strip()).resolve() != repository:
        raise ValueError(f"Not a Git repository root: {repository}")
    return repository


def require_program(name: str) -> None:
    path = shutil.which(name)
    if path is None or not os.access(path, os.X_OK):
        raise ValueError(f"Required executable is unavailable: {name}")


def runtime_paths(repository: Path) -> tuple[Path, Path, Path, Path]:
    root = repository / ".artifacts" / "android"
    return root, root / "requests", root / "claimed", root / "results"


def publish_failure(results: Path, request_id: str, state: str, error_code: str, message: str | None = None) -> None:
    secure_runtime_directory(results / request_id)
    fields = {"errorCode": error_code}
    if message:
        fields["message"] = message
    atomic_write_json(results / request_id / "status.json", status_payload(request_id, state, **fields))


def claim_request(path: Path, claimed: Path) -> Path:
    destination = claimed / path.name
    os.rename(path, destination)
    return destination


def pending_requests(requests: Path) -> list[Path]:
    return sorted((entry for entry in requests.iterdir() if entry.name.endswith(".json")), key=lambda item: item.name)


def classify_and_claim(path: Path, claimed: Path, results: Path, session_id: str, *, busy: bool) -> tuple[Path, object] | None:
    request_id = path.name[:-5]
    log(f"Request detected: {request_id}")
    try:
        request = read_secure_request(path)
    except ProtocolError as exc:
        if len(request_id) == 36:
            try:
                publish_failure(results, request_id, "rejected", exc.code, str(exc))
            except OSError:
                pass
        try:
            path.unlink()
        except OSError:
            pass
        log(f"Request rejected: {request_id} ({exc.code})")
        return None
    destination = claim_request(path, claimed)
    if request.broker_session_id != session_id:
        publish_failure(results, request.request_id, "rejected", "broker_restarted")
        log(f"Request rejected: {request.request_id} (broker_restarted)")
        return None
    if busy:
        publish_failure(results, request.request_id, "rejected", "busy")
        log(f"Request rejected: {request.request_id} (busy)")
        return None
    log(f"Request accepted: {request.request_id}")
    return destination, request


def cplt_command(repository: Path, request_path: Path, request_id: str, session_id: str, build_script: str, build_args: list[str], artifact: str, android_sdk: Path | None) -> list[str]:
    command = [
        "cplt", "--agent", "shell", "--project-dir", os.fspath(repository), "--allow-read", os.fspath(BROKER_ROOT),
    ]
    if android_sdk:
        command.extend(("--allow-read", os.fspath(android_sdk)))
    command.extend([
        "--allow-localhost-any", "exec", "--", "python3", os.fspath(BROKER_ROOT / "worker.py"),
        "--repository", os.fspath(repository), "--request", os.fspath(request_path), "--request-id", request_id,
        "--session-id", session_id, "--build-script", build_script,
    ])
    for argument in build_args:
        command.append(f"--build-arg={argument}")
    command.extend(("--artifact", artifact))
    if android_sdk:
        command.extend(("--android-sdk", os.fspath(android_sdk)))
    return command


def heartbeat(root: Path, repository: Path, session_id: str) -> None:
    atomic_write_json(root / "heartbeat.json", {
        "protocolVersion": PROTOCOL_VERSION, "brokerSessionId": session_id, "targetRepository": os.fspath(repository),
        "pid": os.getpid(), "updatedAt": int(time.time()),
    }, mode=0o644)


def run_build(command: list[str], root: Path, repository: Path, requests: Path, claimed: Path, results: Path, session_id: str, request_id: str) -> None:
    log(f"Build started: {request_id}")
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as exc:
        publish_failure(results, request_id, "failed", "worker_failed", f"Could not launch cplt: {exc}")
        log(f"Build failed: {request_id} (worker_failed)")
        return
    deadline = time.monotonic() + 30 * 60 + 30
    while process.poll() is None:
        heartbeat(root, repository, session_id)
        for path in pending_requests(requests):
            classify_and_claim(path, claimed, results, session_id, busy=True)
        if Stopping.requested:
            terminate_process_group(process)
            publish_failure(results, request_id, "failed", "broker_stopped")
            log(f"Build failed: {request_id} (broker_stopped)")
            return
        if time.monotonic() >= deadline:
            terminate_process_group(process)
            publish_failure(results, request_id, "failed", "build_timeout")
            log(f"Build failed: {request_id} (build_timeout)")
            return
        time.sleep(1)
    status_path = results / request_id / "status.json"
    try:
        state = json.loads(status_path.read_text(encoding="utf-8")).get("state")
    except (OSError, json.JSONDecodeError):
        state = None
    if process.returncode != 0 or state not in TERMINAL_STATES:
        publish_failure(results, request_id, "failed", "worker_failed", f"Worker exited with status {process.returncode}")
        log(f"Build failed: {request_id} (worker_failed)")
        return
    status = json.loads(status_path.read_text(encoding="utf-8"))
    detail = f" ({status['errorCode']})" if "errorCode" in status else ""
    log(f"Build finished: {request_id} ({status['state']}){detail}")


def retain_results(results: Path, now: int | None = None) -> None:
    current = int(time.time()) if now is None else now
    terminal: list[tuple[int, Path]] = []
    if not results.exists():
        return
    for entry in results.iterdir():
        try:
            info = entry.lstat()
            status_path = entry / "status.json"
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                continue
            status_info = status_path.lstat()
            if not stat.S_ISREG(status_info.st_mode) or stat.S_ISLNK(status_info.st_mode):
                continue
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if status.get("state") in TERMINAL_STATES and type(status.get("updatedAt")) is int:
                terminal.append((status["updatedAt"], entry))
        except (OSError, json.JSONDecodeError):
            continue
    terminal.sort(key=lambda item: item[0])
    delete = {path for timestamp, path in terminal if current - timestamp > RETENTION_SECONDS}
    survivors = [(timestamp, path) for timestamp, path in terminal if path not in delete]
    delete.update(path for _timestamp, path in survivors[:-RETENTION_COUNT])
    for path in delete:
        shutil.rmtree(path)


def acquire_lock(repository: Path, session_id: str):
    runtime = BROKER_ROOT / ".runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    lock_file = (runtime / "broker.lock").open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise RuntimeError("Another Android build broker is already running") from exc
    atomic_write_json(runtime / "broker.json", {"pid": os.getpid(), "brokerSessionId": session_id, "targetRepository": os.fspath(repository)}, mode=0o644)
    return lock_file


def run_broker(args: argparse.Namespace) -> int:
    for program in ("python3", "git", "cplt"):
        require_program(program)
    repository = canonical_git_repository(args.repository)
    build_script = args.build_script
    build_args = args.build_arg
    artifact = args.artifact
    android_sdk_value = args.android_sdk
    if repository.name == TRUSTED_DEFAULT_REPOSITORY_NAME:
        build_script = build_script or "scripts/build-android-smoke-apk.sh"
        build_args = build_args if build_args is not None else ["all"]
        artifact = artifact or ".artifacts/android/trene.apk"
        android_sdk_value = android_sdk_value or "~/Library/Android/sdk"
    elif not build_script or build_args is None or not artifact:
        raise ValueError("Non-trene repositories require --build-script, at least one --build-arg, and --artifact")
    repository_path(repository, build_script, executable=True)
    repository_path(repository, artifact)
    android_sdk = Path(android_sdk_value).expanduser().resolve(strict=True) if android_sdk_value else None
    if android_sdk and not android_sdk.is_dir():
        raise ValueError(f"Android SDK is not a directory: {android_sdk}")
    root, requests, claimed, results = runtime_paths(repository)
    secure_runtime_directory(root)
    for directory in (requests, claimed, results):
        secure_runtime_directory(directory)
    session_id = str(uuid.uuid4())
    lock_file = acquire_lock(repository, session_id)
    signal.signal(signal.SIGINT, lambda _signum, _frame: setattr(Stopping, "requested", True))
    signal.signal(signal.SIGTERM, lambda _signum, _frame: setattr(Stopping, "requested", True))
    log(f"Broker started: session {session_id}, target {repository}")
    log("Warning: builds can access arbitrary localhost services while cplt is running")
    try:
        retain_results(results)
        for old in pending_requests(requests):
            classify_and_claim(old, claimed, results, session_id, busy=False)
        for old in pending_requests(claimed):
            try:
                request = read_secure_request(old)
                publish_failure(results, request.request_id, "rejected", "broker_restarted")
            except ProtocolError:
                pass
        while not Stopping.requested:
            heartbeat(root, repository, session_id)
            selected = None
            for path in pending_requests(requests):
                outcome = classify_and_claim(path, claimed, results, session_id, busy=selected is not None)
                if outcome and selected is None:
                    selected = outcome
            if selected:
                request_path, request = selected
                run_build(cplt_command(repository, request_path, request.request_id, session_id, build_script, build_args, artifact, android_sdk), root, repository, requests, claimed, results, session_id, request.request_id)
                retain_results(results)
            else:
                time.sleep(1)
    finally:
        try:
            (root / "heartbeat.json").unlink()
        except FileNotFoundError:
            pass
        lock_file.close()
        log("Broker stopped")
    return 0


def generated_client() -> str:
    template = (BROKER_ROOT / "request_client.py").read_text(encoding="utf-8")
    source = (BROKER_ROOT / "source_identity.py").read_text(encoding="utf-8")
    source = source.replace("#!/usr/bin/env python3\n", "", 1)
    return template.replace("SOURCE_IDENTITY_PLACEHOLDER", source.replace("\\", "\\\\").replace("'''", "\\'\\'\\'"))


def init_repo(args: argparse.Namespace) -> int:
    repository = canonical_git_repository(args.repository)
    client_path = repository / "scripts" / "request-android-build.py"
    scripts_path = client_path.parent
    if scripts_path.is_symlink() or (scripts_path.exists() and not scripts_path.is_dir()):
        raise ValueError(f"Refusing unsafe scripts path: {scripts_path}")
    if client_path.is_symlink():
        raise ValueError(f"Refusing symlinked client: {client_path}")
    desired = generated_client()
    if client_path.exists() and client_path.read_text(encoding="utf-8") != desired:
        raise ValueError(f"Refusing to overwrite a modified or unknown client: {client_path}")
    ignore_path = repository / ".gitignore"
    if ignore_path.is_symlink():
        raise ValueError(f"Refusing symlinked .gitignore: {ignore_path}")
    old_ignore = ignore_path.read_text(encoding="utf-8") if ignore_path.exists() else ""
    equivalent = any(line.strip() in {".artifacts/", "/.artifacts/", ".artifacts", "/.artifacts"} for line in old_ignore.splitlines())
    changes = [f"install/update {client_path.relative_to(repository)}"]
    if not equivalent:
        changes.append("append /.artifacts/ to .gitignore")
    print("Proposed changes:")
    for change in changes:
        print(f"  - {change}")
    if input("Apply these changes? [y/N] ").strip().lower() not in {"y", "yes"}:
        print("No changes made.")
        return 1
    client_path.parent.mkdir(parents=True, exist_ok=True)
    client_fd = os.open(client_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o755)
    with os.fdopen(client_fd, "w", encoding="utf-8") as client_file:
        client_file.write(desired)
    client_path.chmod(0o755)
    if not equivalent:
        separator = "" if not old_ignore or old_ignore.endswith("\n") else "\n"
        ignore_fd = os.open(ignore_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(ignore_fd, "w", encoding="utf-8") as ignore_file:
            ignore_file.write(old_ignore + separator + "/.artifacts/\n")
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the foreground broker")
    serve.add_argument("repository")
    serve.add_argument("--build-script")
    serve.add_argument("--build-arg", action="append")
    serve.add_argument("--artifact")
    serve.add_argument("--android-sdk", help="trusted Android SDK directory; defaults for trene only")
    initialize = commands.add_parser("init-repo", help="install the generated request client")
    initialize.add_argument("repository")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        return init_repo(args) if args.command == "init-repo" else run_broker(args)
    except (OSError, ValueError, RuntimeError, ProtocolError) as exc:
        print(f"broker: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
