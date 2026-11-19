"""Port quarry's final-record redacting formatter (MIT, Punt Labs)."""

from __future__ import annotations

import copy
import logging

from punt_observe.logging.redactor import LogRedactor


def bounded(text: str, limit: int = 65_535) -> str:
    """Bound final UTF-8 output, reserving one byte for the sink newline."""
    raw = text.encode("utf-8", errors="backslashreplace")
    if len(raw) <= limit:
        return raw.decode("utf-8")
    marker = b" [truncated]"
    return raw[: limit - len(marker)].decode("utf-8", errors="ignore") + marker.decode()


class RedactingFormatter(logging.Formatter):
    """Redact formatted records, including indented exception continuations."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a copy, withholding records on unsettled or failed redaction."""
        try:
            single = copy.copy(record)
            single.msg, single.args = LogRedactor.one_line(record.getMessage()), ()
            single.name = LogRedactor.one_line(record.name)
            single.exc_text = None
            if record.exc_info:
                single.exc_text = LogRedactor.continuation(
                    self.formatException(record.exc_info)
                )
            elif record.exc_text:
                single.exc_text = LogRedactor.continuation(record.exc_text)
            single.exc_info = None
            if record.stack_info:
                single.stack_info = LogRedactor.continuation(record.stack_info)
            settled = LogRedactor.settle(super().format(single))
            if settled is not None:
                return bounded(settled)
        except Exception:
            pass
        return "[record withheld: redaction did not settle]"
