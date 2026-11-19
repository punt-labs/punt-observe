"""Adapt quarry's reference handler test cases to the shared formatter."""

from __future__ import annotations

import logging
from typing import TextIO

from punt_observe.logging.formatter import RedactingFormatter


class RedactingStreamHandler(logging.StreamHandler[TextIO]):
    def format(self, record: logging.LogRecord) -> str:
        return RedactingFormatter().format(record)
