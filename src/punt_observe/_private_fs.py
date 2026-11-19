"""Secure owner-only regular files before opening diagnostic or telemetry sinks."""

from __future__ import annotations

import os
import re
import stat
import tempfile
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
        """Create and tighten private directories without following symlink ancestors.

        Raises:
            OSError: The directory cannot be secured.
        """
        fd = PrivatePaths._directory_fd(path, create=True)
        os.close(fd)

    @classmethod
    def _directory_fd(cls, path: Path, *, create: bool) -> int:
        path = path.absolute()
        if ".." in path.parts:
            raise OSError("parent traversal is not allowed")
        anchors = (
            Path.home(),
            Path(tempfile.gettempdir()),
            Path("/tmp"),
            Path("/var/tmp"),
        )
        anchor = max(
            (
                p.absolute()
                for p in anchors
                if path.is_relative_to(p.absolute()) and p.is_dir()
            ),
            key=lambda p: len(p.parts),
            default=Path("/"),
        )
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(anchor.resolve(strict=True), flags)
        current = anchor
        root = cls.root().absolute()
        try:
            for part in path.relative_to(anchor).parts:
                current /= part
                created = False
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                        created = True
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                if created or current in (path, root) or root in current.parents:
                    info = os.fstat(fd)
                    if info.st_uid != os.getuid():
                        raise PermissionError(
                            "directory is not owned by the current user"
                        )
                    if stat.S_IMODE(info.st_mode) != 0o700:
                        os.fchmod(fd, 0o700)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def ensure_private_tree(self) -> None:
        """Secure every owned namespace level through the immediate parent.

        Raises:
            OSError: The directory cannot be secured.
        """
        self.ensure_directory(self._path.parent)

    def open_private(self, flags: int) -> int:
        """Open a regular file privately and tighten the returned descriptor.

        Raises:
            OSError: Opening or tightening the file failed.
        """
        parent = self._directory_fd(self._path.parent, create=False)
        try:
            fd = os.open(
                self._path.name,
                flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=parent,
            )
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular file")
            if info.st_uid != os.getuid():
                raise PermissionError("file is not owned by the current user")
            if not flags & os.O_NONBLOCK:
                os.set_blocking(fd, True)
            if stat.S_IMODE(info.st_mode) != 0o600:
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
            try:
                fd = self.open_private(os.O_WRONLY)
            except FileNotFoundError:
                return
        os.close(fd)
