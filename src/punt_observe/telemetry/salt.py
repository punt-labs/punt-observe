"""Derive install-private identifiers using an atomically linked random salt."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from punt_observe._private_fs import PrivatePaths
from punt_observe.telemetry.schema import IdentifierHash


def load_salt() -> bytes:
    """Create or read a private 32-byte per-install salt without replacement.

    Raises:
        OSError: The salt cannot be secured, read or atomically installed.
        ValueError: Existing salt has an invalid length.
    """
    root = PrivatePaths.root()
    PrivatePaths.ensure_directory(root)
    path = root / "telemetry.salt"
    temporary = root / f".salt-{secrets.token_hex(16)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        salt = secrets.token_bytes(32)
        if os.write(fd, salt) != 32:
            raise OSError("short salt write")
        os.fsync(fd)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)
    fd = PrivatePaths(path).open_private(os.O_RDONLY)
    try:
        salt = os.read(fd, 33)
    finally:
        os.close(fd)
    if len(salt) != 32:
        raise ValueError("invalid salt length")
    return salt


def hash_identifier(value: str) -> IdentifierHash:
    """Derive a 128-bit HMAC identifier without retaining the input.

    Raises:
        OSError: Salt storage is unavailable.
        ValueError: The stored salt is invalid.
    """
    return IdentifierHash(hmac.new(load_salt(), value.encode(), hashlib.sha256).hexdigest()[:32])
