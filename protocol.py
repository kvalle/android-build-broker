#!/usr/bin/env python3
"""Stable request/status protocol and safe local file operations."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_REQUEST_SIZE = 16 * 1024
MAX_REQUEST_AGE = 30 * 60
HEARTBEAT_MAX_AGE = 5
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
OID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_STATES = frozenset({"passed", "failed", "stale", "rejected"})
REQUEST_KEYS = frozenset({"protocolVersion", "requestId", "createdAt", "brokerSessionId", "head", "worktreeDigest"})


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Request:
    request_id: str
    created_at: int
    broker_session_id: str
    head: str
    worktree_digest: str


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("invalid_request", f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def parse_request_bytes(data: bytes, filename: str, now: int | None = None) -> Request:
    if len(data) > MAX_REQUEST_SIZE:
        raise ProtocolError("request_too_large", "Request exceeds 16 KiB")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except ProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid_request", "Request is not valid UTF-8 JSON") from exc
    if type(value) is not dict or frozenset(value) != REQUEST_KEYS:
        raise ProtocolError("invalid_request", "Request has unknown or missing keys")
    if type(value["protocolVersion"]) is not int or value["protocolVersion"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_protocol", "Unsupported protocol version")
    request_id = value["requestId"]
    if type(request_id) is not str or not UUID_PATTERN.fullmatch(request_id) or filename != f"{request_id}.json":
        raise ProtocolError("invalid_request", "Request ID and filename must be the same lowercase UUIDv4")
    if type(value["brokerSessionId"]) is not str or not UUID_PATTERN.fullmatch(value["brokerSessionId"]):
        raise ProtocolError("invalid_request", "Invalid broker session ID")
    if type(value["head"]) is not str or not OID_PATTERN.fullmatch(value["head"]):
        raise ProtocolError("invalid_request", "Invalid HEAD object ID")
    if type(value["worktreeDigest"]) is not str or not SHA256_PATTERN.fullmatch(value["worktreeDigest"]):
        raise ProtocolError("invalid_request", "Invalid worktree digest")
    created_at = value["createdAt"]
    current = int(time.time()) if now is None else now
    if type(created_at) is not int or created_at > current + 30:
        raise ProtocolError("invalid_request", "Invalid request creation time")
    if current - created_at > MAX_REQUEST_AGE:
        raise ProtocolError("request_expired", "Request is older than 30 minutes")
    return Request(request_id, created_at, value["brokerSessionId"], value["head"], value["worktreeDigest"])


def read_secure_request(path: Path, now: int | None = None) -> Request:
    directory_fd = open_directory_nofollow(path.parent)
    try:
        try:
            info = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise ProtocolError("invalid_request_file", f"Cannot inspect request: {exc}") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise ProtocolError("invalid_request_file", "Request must be an owned 0600 ordinary file with one link")
        if info.st_size > MAX_REQUEST_SIZE:
            raise ProtocolError("request_too_large", "Request exceeds 16 KiB")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path.name, flags, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as request_file:
                opened = os.fstat(request_file.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ProtocolError("invalid_request_file", "Request changed while opening")
                data = request_file.read(MAX_REQUEST_SIZE + 1)
        except OSError as exc:
            raise ProtocolError("invalid_request_file", f"Cannot read request: {exc}") from exc
    finally:
        os.close(directory_fd)
    return parse_request_bytes(data, path.name, now)


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    directory_fd = open_directory_nofollow(path.parent)
    temporary = f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=directory_fd)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(directory_fd)


def open_regular_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    directory_fd = open_directory_nofollow(path.parent)
    try:
        fd = os.open(path.name, flags | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise OSError(f"Not an ordinary file: {path}")
    return fd


def open_directory_nofollow(path: Path) -> int:
    absolute = path.absolute()
    parts = absolute.parts
    runtime_index = next((index for index, component in enumerate(parts) if component in {".artifacts", ".runtime"}), len(parts))
    anchor = Path(*parts[:runtime_index]) if runtime_index else Path(absolute.anchor)
    current_fd = os.open(anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for component in parts[runtime_index:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def status_payload(request_id: str, state: str, **fields: Any) -> dict[str, Any]:
    return {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "state": state, "updatedAt": int(time.time()), **fields}


def secure_runtime_directory(path: Path) -> None:
    absolute = path.absolute()
    parts = absolute.parts
    runtime_index = next((index for index, component in enumerate(parts) if component in {".artifacts", ".runtime"}), len(parts) - 1)
    anchor = Path(*parts[:runtime_index]) if runtime_index else Path(absolute.anchor)
    current_fd = os.open(anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    current = anchor
    try:
        for component in parts[runtime_index:]:
            current /= component
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=current_fd)
                next_fd = os.open(
                    component,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
            info = os.fstat(next_fd)
            if current == absolute and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
                os.close(next_fd)
                raise ProtocolError("unsafe_runtime_path", f"Runtime directory has unsafe ownership or mode: {current}")
            os.close(current_fd)
            current_fd = next_fd
    except OSError as exc:
        raise ProtocolError("unsafe_runtime_path", f"Unsafe runtime path {current}: {exc}") from exc
    finally:
        os.close(current_fd)
