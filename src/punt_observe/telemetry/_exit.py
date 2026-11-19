"""Bound process-exit flushing through one registry and prestarted daemons."""

from __future__ import annotations

import atexit
import threading
import time
from collections.abc import Callable

from punt_observe.telemetry._diagnostics import warn

_TIMEOUT_S = 2.0
_lock = threading.Lock()
_pending: set[ExitFlush] = set()


class ExitFlush:
    """Retain pending providers and prepare cleanup before Python stops threads."""

    def __init__(self, shutdown: Callable[[float], None]) -> None:
        self._shutdown = shutdown
        self._ready = threading.Event()
        self._done = threading.Event()
        self._cancelled = False
        self._deadline = 0.0
        self._thread = threading.Thread(
            target=self._run, name="punt-observe-exit", daemon=True
        )
        self._thread.start()
        with _lock:
            _pending.add(self)

    def _run(self) -> None:
        self._ready.wait()
        try:
            if not self._cancelled:
                self._shutdown(max(0.0, self._deadline - time.monotonic()))
        except BaseException:
            warn("exit_flush_failed")
        finally:
            self._done.set()

    def cancel(self) -> None:
        """Forget explicit shutdown and release its unused exit worker."""
        with _lock:
            _pending.discard(self)
        self._cancelled = True
        self._ready.set()

    def request(self, deadline: float) -> None:
        """Wake an already-started daemon without creating a thread at atexit."""
        self._deadline = deadline
        self._ready.set()

    def wait(self, deadline: float) -> None:
        """Wait only for the remainder of the shared process-exit budget."""
        self._done.wait(max(0.0, deadline - time.monotonic()))


def reset_after_fork() -> None:
    """Discard parent-only work without touching inherited synchronization."""
    global _lock, _pending
    _lock = threading.Lock()
    _pending = set()


def _shutdown_at_exit() -> None:
    deadline = time.monotonic() + _TIMEOUT_S
    if not _lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        return
    try:
        pending = tuple(_pending)
        _pending.clear()
    finally:
        _lock.release()
    for task in pending:
        task.request(deadline)
    for task in pending:
        task.wait(deadline)


atexit.register(_shutdown_at_exit)
