"""Configure process-owned durable diagnostic sinks with dictConfig."""

from __future__ import annotations

import logging
import logging.config
import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

from punt_observe._private_fs import PrivatePaths
from punt_observe.logging.append_log import AtomicAppendLog
from punt_observe.logging.formatter import RedactingFormatter
from punt_observe.logging.handler import PrivateFileHandler, RedactingStreamHandler

logger = logging.getLogger(__name__)


class Role(StrEnum):
    """Identify a process entry point's diagnostic role."""

    CLI = "cli"
    MCP = "mcp"
    HOOK = "hook"
    DAEMON = "daemon"
    WORKER = "worker"


class LoggingSetupError(OSError):
    """Report a durable sink whose startup permissions could not be secured."""


def configure_logging(
    tool: str,
    role: Role | str,
    *,
    stderr_level: str = "WARNING",
    level: str = "INFO",
    quiet: tuple[str, ...] = ("mcp", "httpx", "httpcore", "urllib3", "asyncio"),
) -> Path:
    """Replace diagnostic handlers and return the private durable log path.

    Raises:
        ValueError: The tool, role or logging level is invalid.
        LoggingSetupError: Startup cannot establish private sink permissions.
    """
    role = Role(role)
    path = PrivatePaths.root(tool) / "logs" / f"{tool}.log"
    try:
        PrivatePaths(path).secure()
        failures = AtomicAppendLog(path).tighten_existing()
        if failures:
            raise OSError("cannot tighten existing diagnostic files")
    except OSError as exc:
        raise LoggingSetupError("cannot establish private diagnostic sink") from exc
    handlers: dict[str, Any] = {
        "file": {
            "()": PrivateFileHandler,
            "path": path,
            "level": level,
            "formatter": "standard",
        }
    }
    if sys.stderr is not None:
        handlers["stderr"] = {
            "()": RedactingStreamHandler,
            "stream": "ext://sys.stderr",
            "level": stderr_level,
            "formatter": "standard",
        }
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "()": RedactingFormatter,
                    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                }
            },
            "handlers": handlers,
            "loggers": {name: {"level": "WARNING"} for name in quiet},
            "root": {"level": "DEBUG", "handlers": list(handlers)},
        }
    )
    fallback: logging.Handler = (
        RedactingStreamHandler(sys.stderr)
        if sys.stderr is not None
        else logging.NullHandler()
    )
    fallback.setLevel(logging.WARNING)
    fallback.setFormatter(
        RedactingFormatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
        )
    )
    logging.lastResort = fallback
    startup = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        0,
        "Configured %s diagnostics (pid=%d)",
        (role, os.getpid()),
        None,
    )
    file_handler = logging.getHandlerByName("file")
    assert file_handler is not None
    file_handler.handle(startup)
    stderr_handler = logging.getHandlerByName("stderr")
    if stderr_handler is not None and startup.levelno >= stderr_handler.level:
        stderr_handler.handle(startup)
    return path
