#!/usr/bin/env python3
"""Deterministic identity for a Git HEAD and its non-ignored worktree."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass


class SourceIdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceIdentity:
    head: str
    digest: str


def _git(repo: bytes, *args: bytes) -> bytes:
    command = [b"git", b"-C", repo, b"-c", b"core.quotepath=false", *args]
    try:
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"").decode("utf-8", "replace").strip()
        raise SourceIdentityError(detail or f"Git command failed: {args!r}") from exc
    return result.stdout


def _snapshot(repo: bytes) -> tuple[bytes, tuple[bytes, ...]]:
    head = _git(repo, b"rev-parse", b"--verify", b"HEAD").strip()
    if len(head) not in (40, 64) or any(byte not in b"0123456789abcdef" for byte in head):
        raise SourceIdentityError("HEAD is not a canonical object ID")
    raw = _git(repo, b"ls-files", b"-z", b"--cached", b"--others", b"--exclude-standard", b"--")
    paths = tuple(sorted(set(path for path in raw.split(b"\0") if path)))
    return head, paths


def _field(digest: object, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def compute_source_identity(repo: str | bytes) -> SourceIdentity:
    repo_bytes = os.fsencode(os.path.realpath(repo))
    for _attempt in range(3):
        head, paths = _snapshot(repo_bytes)
        digest = hashlib.sha256(b"android-build-broker-source-v1\0")
        _field(digest, head)
        unstable = False
        for relative in paths:
            if relative == b".artifacts" or relative.startswith(b".artifacts/"):
                continue
            if relative == b".git" or relative.startswith(b".git/") or relative.startswith(b"/") or b"\0" in relative:
                raise SourceIdentityError("Git returned an unsafe path")
            full_path = repo_bytes + b"/" + relative
            _field(digest, relative)
            try:
                before = os.lstat(full_path)
            except FileNotFoundError:
                digest.update(b"deleted\0")
                continue
            if stat.S_ISLNK(before.st_mode):
                digest.update(b"symlink\0")
                _field(digest, os.readlink(full_path))
                try:
                    unstable = os.lstat(full_path) != before
                except FileNotFoundError:
                    unstable = True
                if unstable:
                    break
                continue
            if not stat.S_ISREG(before.st_mode):
                raise SourceIdentityError(f"Unsupported worktree file type: {os.fsdecode(relative)!r}")
            digest.update(b"100755\0" if before.st_mode & 0o111 else b"100644\0")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(full_path, flags)
                with os.fdopen(fd, "rb", closefd=True) as source:
                    opened = os.fstat(source.fileno())
                    if (opened.st_dev, opened.st_ino, opened.st_mode) != (before.st_dev, before.st_ino, before.st_mode):
                        unstable = True
                        break
                    digest.update(before.st_size.to_bytes(8, "big"))
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                    after = os.fstat(source.fileno())
            except (FileNotFoundError, OSError):
                unstable = True
                break
            stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(before, name) != getattr(after, name) for name in stable_fields):
                unstable = True
                break
        if not unstable and _snapshot(repo_bytes) == (head, paths):
            return SourceIdentity(head.decode("ascii"), digest.hexdigest())
    raise SourceIdentityError("Worktree changed while its source identity was computed")
