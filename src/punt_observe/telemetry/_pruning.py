"""Run local retention independently of signal export on a private daemon."""

from __future__ import annotations

import math
import threading

from punt_observe.telemetry._diagnostics import warn
from punt_observe.telemetry.sqlite_exporter import SQLiteStore


class PeriodicPruner:
    """Own interruptible retention scheduling and isolate maintenance failures."""

    def __init__(self, store: SQLiteStore, interval_s: float) -> None:
        if not math.isfinite(interval_s) or not 0 < interval_s <= threading.TIMEOUT_MAX:
            raise ValueError("invalid pruning interval")
        self._store = store
        self._interval_s = interval_s
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="punt-observe-prune", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stopped.wait(self._interval_s):
            try:
                self._store.prune()
            except Exception:
                warn("sqlite_prune_failed")

    def stop(self) -> None:
        """Cancel future maintenance without waiting for in-flight SQLite work."""
        self._stopped.set()

    def join(self) -> None:
        """Finish in-flight maintenance before the provider closes shared storage."""
        self._thread.join()
