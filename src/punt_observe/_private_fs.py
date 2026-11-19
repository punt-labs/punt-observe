"""Secure owner-only regular files before opening diagnostic or telemetry sinks."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Self


class PrivatePaths:
    """Prepare private directories and files, refusing symlink leaves."""

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    @staticmethod
    def root(tool: str | None = None) -> Path:
        """Resolve the home namespace, rejecting unsafe tool names.

        Raises:
            ValueError: The tool name is not a bounded lowercase slug.
        """
        root = Path.home() / ".punt-labs"
        if tool is None:
            return root
        if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", tool) is None:
            raise ValueError("invalid tool name")
        return root / tool

    @classmethod
    def for_append_sink(cls, path: Path) -> PrivatePaths:
        """Bind the private path used by vox's append protocol."""
        return cls(path)

    @staticmethod
    def ensure_directory(path: Path) -> None:
        """Create and tighten a directory without following its leaf.

        Raises:
            OSError: The directory cannot be secured.
        """
        if not path.exists():
            if not path.parent.exists():
                PrivatePaths.ensure_directory(path.parent)
            path.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)

    def ensure_private_tree(self) -> None:
        """Secure the immediate parent directory.

        Raises:
            OSError: The directory cannot be secured.
        """
        self.ensure_directory(self._path.parent)

    def open_private(self, flags: int) -> int:
        """Open a regular file privately and tighten the returned descriptor.

        Raises:
            OSError: Opening or tightening the file failed.
        """
        fd = os.open(self._path, flags | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("not a regular file")
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def secure(self) -> None:
        """Precreate a private leaf or tighten the existing regular file.

        Raises:
            OSError: The directory or file cannot be secured.
        """
        self.ensure_private_tree()
        try:
            fd = self.open_private(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            fd = self.open_private(os.O_WRONLY)
        os.close(fd)
