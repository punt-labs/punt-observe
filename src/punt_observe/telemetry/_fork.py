"""Reset live private telemetry through one process-wide child hook."""

from __future__ import annotations

import os
import threading
from typing import Protocol
from weakref import WeakSet

from punt_observe.telemetry._diagnostics import reset_after_fork as reset_diagnostics
from punt_observe.telemetry._diagnostics import warn


class _ForkAware(Protocol):
    def reset_after_fork(self) -> None: ...


_instances: WeakSet[_ForkAware] = WeakSet()
_lock = threading.Lock()


def register(instance: _ForkAware) -> None:
    """Weakly track a live instance without adding another process-wide hook."""
    with _lock:
        _instances.add(instance)


def unregister(instance: _ForkAware) -> None:
    """Stop resetting an instance as soon as shutdown begins."""
    with _lock:
        _instances.discard(instance)


def _after_fork() -> None:
    global _lock
    _lock = threading.Lock()
    reset_diagnostics()
    for instance in tuple(_instances):
        try:
            instance.reset_after_fork()
        except Exception:
            warn("fork_reset_failed")


os.register_at_fork(after_in_child=_after_fork)
