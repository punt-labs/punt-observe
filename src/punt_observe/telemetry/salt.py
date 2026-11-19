"""Derive install-private identifiers using an atomically linked random salt."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from contextlib import suppress
from pathlib import Path

from punt_observe._private_fs import PrivatePaths
from punt_observe.telemetry._hash_provenance import HASH_TOKEN
from punt_observe.telemetry.schema import IdentifierHash

_lock = threading.Lock()
_cached: tuple[Path, tuple[int, ...], bytes] | None = None


def _read_salt(path: Path) -> bytes:
    global _cached
    fd = PrivatePaths(path).open_private(os.O_RDONLY)
    try:
        info = os.fstat(fd)
        fingerprint = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        if _cached is not None and _cached[:2] == (path, fingerprint):
            return _cached[2]
        salt = os.read(fd, 33)
    finally:
        os.close(fd)
    if len(salt) != 32:
        raise ValueError("invalid salt length")
    _cached = path, fingerprint, salt
    return salt


def _create_salt(path: Path) -> None:
    temporary = path.parent / f".salt-{secrets.token_hex(16)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    linked = False
    try:
        salt = secrets.token_bytes(32)
        if os.write(fd, salt) != 32:
            raise OSError("short salt write")
        os.fsync(fd)
        with suppress(FileExistsError):
            os.link(temporary, path)
            linked = True
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)
    if linked:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def load_salt() -> bytes:
    """Create or read a private 32-byte per-install salt without replacement.

    Raises:
        OSError: The salt cannot be secured, read or atomically installed.
        ValueError: Existing salt has an invalid length.
    """
    with _lock:
        root = PrivatePaths.root()
        PrivatePaths.ensure_directory(root)
        path = root / "telemetry.salt"
        try:
            return _read_salt(path)
        except FileNotFoundError:
            _create_salt(path)
            return _read_salt(path)


def hash_identifier(value: str) -> IdentifierHash:
    """Derive a 128-bit HMAC identifier without retaining the input.

    Raises:
        OSError: Salt storage is unavailable.
        ValueError: The stored salt is invalid.
    """
    return IdentifierHash(
        hmac.new(load_salt(), value.encode(), hashlib.sha256).hexdigest()[:32],
        _proof=HASH_TOKEN,
    )
