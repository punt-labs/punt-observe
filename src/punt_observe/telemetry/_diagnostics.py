"""Rate-limit fixed, metadata-only failure diagnostics outside telemetry."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import suppress

logger = logging.getLogger("punt_observe.telemetry")
_lock = threading.Lock()
_last = 0.0


def _after_fork() -> None:
    global _lock, _last
    _lock = threading.Lock()
    _last = 0.0


os.register_at_fork(after_in_child=_after_fork)


def warn(reason: str) -> None:
    """Emit a bounded-rate reason code, never failed payloads or exceptions."""
    global _last
    now = time.monotonic()
    with _lock:
        if now - _last < 60:
            return
        _last = now
    with suppress(Exception):
        logger.warning("Telemetry dropped: %s", reason)
