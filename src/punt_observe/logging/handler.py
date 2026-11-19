"""Write formatted private diagnostics through the atomic append sink."""

from __future__ import annotations

import logging
from pathlib import Path

from punt_observe.logging.append_log import AtomicAppendLog
from punt_observe.logging.formatter import bounded


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
            from punt_observe.logging.sanitize import escape

            self.sink.append(bounded(escape(from_text)))
        except Exception:
            AtomicAppendLog._to_stderr("record withheld: logging failure")
