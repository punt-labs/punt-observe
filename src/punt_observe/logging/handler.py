"""Write formatted private diagnostics through the atomic append sink."""

from __future__ import annotations

import logging
import sys
from contextlib import suppress
from pathlib import Path
from typing import TextIO

from punt_observe.logging.append_log import AtomicAppendLog
from punt_observe.logging.formatter import bounded
from punt_observe.logging.sanitize import escape


def _report_failure() -> None:
    with suppress(Exception):
        if sys.__stderr__ is not None:
            sys.__stderr__.write("punt-observe: record withheld: logging failure\n")


class RedactingStreamHandler(logging.StreamHandler[TextIO]):
    """Keep raw records out of Python's default stream failure report."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        """Report fixed metadata only, never original arguments or exceptions."""
        _report_failure()


class PrivateFileHandler(logging.Handler):
    """Append redacted records without recursive logging on sink errors."""

    def __init__(
        self, path: Path, *, max_bytes: int = 5_242_880, backup_count: int = 5
    ) -> None:
        super().__init__()
        self.sink = AtomicAppendLog(
            path, max_bytes=max_bytes, backup_count=backup_count
        )

    def emit(self, record: logging.LogRecord) -> None:
        """Write one escaped physical line, including a bounded terminal newline."""
        try:
            from_text = self.format(record)
            # Escape first: the expansion must count against the byte budget.
            self.sink.append(bounded(escape(from_text)))
        except Exception:
            _report_failure()
