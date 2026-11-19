"""Export validated OTel signals to bounded private local SQLite storage."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from punt_observe._private_fs import PrivatePaths
from punt_observe.telemetry._diagnostics import warn

_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spans(
 trace_id TEXT, span_id TEXT, parent_span_id TEXT, name TEXT,
 schema_version INTEGER, kind TEXT, start_ns INTEGER, end_ns INTEGER,
 status TEXT, attributes TEXT);
CREATE TABLE IF NOT EXISTS span_events(
 span_id TEXT, name TEXT, schema_version INTEGER, ts_ns INTEGER, attributes TEXT);
CREATE TABLE IF NOT EXISTS metric_points(
 name TEXT, schema_version INTEGER, kind TEXT, unit TEXT, start_ns INTEGER,
 ts_ns INTEGER, value REAL, count INTEGER, sum REAL, attributes TEXT);
CREATE INDEX IF NOT EXISTS spans_end ON spans(end_ns);
CREATE INDEX IF NOT EXISTS events_ts ON span_events(ts_ns);
CREATE INDEX IF NOT EXISTS metrics_ts ON metric_points(ts_ns);
"""


class SQLiteStore:
    """Serialize local OTel export and periodic incremental space reclamation.

    Raises:
        OSError: Private file permissions cannot be established.
        sqlite3.Error: The store cannot be initialized.
        ValueError: Retention or an existing storage version is invalid.
    """

    def __init__(
        self, path: Path, retention_days: int, resource: dict[str, str | int]
    ) -> None:
        if type(retention_days) is not int or not 1 <= retention_days <= 3650:
            raise ValueError("invalid retention")
        self.path = path
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._last_prune = 0.0
        self._closed = False
        PrivatePaths(path).secure()
        self._secure_sidecars()
        self._conn = sqlite3.connect(path, timeout=2, check_same_thread=False)
        try:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported store version")
            self._conn.execute("PRAGMA busy_timeout=2000")
            if version == 0:
                self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_DDL)
            self._conn.execute("PRAGMA user_version=1")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('resource', ?)",
                (json.dumps(resource, sort_keys=True),),
            )
            self._conn.commit()
            self._secure_sidecars()
            self.prune()
        except BaseException:
            self._conn.close()
            raise

    def _secure_sidecars(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            path = self.path.with_name(self.path.name + suffix)
            if path.exists() or path.is_symlink():
                PrivatePaths(path).secure()

    def prune(self) -> None:
        """Delete expired signals and reclaim free pages incrementally."""
        cutoff = time.time_ns() - self.retention_days * 86_400 * 1_000_000_000
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM spans WHERE end_ns < ?", (cutoff,))
            self._conn.execute("DELETE FROM span_events WHERE ts_ns < ?", (cutoff,))
            self._conn.execute("DELETE FROM metric_points WHERE ts_ns < ?", (cutoff,))
        with self._lock:
            self._conn.execute("PRAGMA incremental_vacuum").fetchall()
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._last_prune = time.monotonic()

    def write(self, sql: str, rows: list[tuple[Any, ...]]) -> bool:
        """Write a bounded exporter batch, converting storage errors to failure."""
        return self.write_batch([(sql, rows)])

    def write_batch(
        self, statements: Sequence[tuple[str, list[tuple[Any, ...]]]]
    ) -> bool:
        """Commit all statements together, rolling the entire batch back on error."""
        try:
            with self._lock, self._conn:
                if self._closed:
                    return False
                for sql, rows in statements:
                    self._conn.executemany(sql, rows)
                self._secure_sidecars()
        except Exception:
            warn("sqlite_export_failed")
            return False
        try:
            if time.monotonic() - self._last_prune >= 3600:
                self.prune()
        except Exception:
            warn("sqlite_prune_failed")
        return True

    def close(self) -> None:
        """Close the store once all private providers have stopped."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()


def _attributes(attrs: Any) -> tuple[int, str]:
    values = dict(attrs or {})
    version = int(values.pop("punt.schema_version", 1))
    values.pop("punt.signal", None)
    return version, json.dumps(values, sort_keys=True)


class SQLiteSpanExporter(SpanExporter):
    """Persist span identities, events and typed attributes without content."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Persist a batch or return FAILURE without propagating errors."""
        try:
            rows: list[tuple[Any, ...]] = []
            events: list[tuple[Any, ...]] = []
            for span in spans:
                context = span.context
                if context is None:
                    continue
                version, attrs = _attributes(span.attributes)
                sid = f"{context.span_id:016x}"
                rows.append(
                    (
                        f"{context.trace_id:032x}",
                        sid,
                        f"{span.parent.span_id:016x}" if span.parent else None,
                        span.name,
                        version,
                        (span.attributes or {}).get("punt.signal", "span"),
                        span.start_time,
                        span.end_time,
                        span.status.status_code.name,
                        attrs,
                    )
                )
                for event in span.events:
                    eversion, eattrs = _attributes(event.attributes)
                    events.append((sid, event.name, eversion, event.timestamp, eattrs))
            success = self.store.write_batch(
                [
                    ("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?)", rows),
                    ("INSERT INTO span_events VALUES (?,?,?,?,?)", events),
                ]
            )
            return SpanExportResult.SUCCESS if success else SpanExportResult.FAILURE
        except Exception:
            warn("span_export_failed")
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        """Leave shared store ownership with the provider bundle."""


class SQLiteMetricExporter(MetricExporter):
    """Persist counter and histogram points from the private meter provider."""

    def __init__(self, store: SQLiteStore) -> None:
        super().__init__()
        self.store = store

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: Any,
    ) -> MetricExportResult:
        """Persist metric points or return FAILURE without propagating errors."""
        try:
            rows: list[tuple[Any, ...]] = []
            for resource in metrics_data.resource_metrics:
                for scope in resource.scope_metrics:
                    for metric in scope.metrics:
                        for point in metric.data.data_points:
                            version, attrs = _attributes(point.attributes)
                            histogram = hasattr(point, "count")
                            rows.append(
                                (
                                    metric.name,
                                    version,
                                    "histogram" if histogram else "counter",
                                    metric.unit,
                                    point.start_time_unix_nano,
                                    point.time_unix_nano,
                                    getattr(point, "value", None),
                                    getattr(point, "count", None),
                                    getattr(point, "sum", None),
                                    attrs,
                                )
                            )
            ok = self.store.write(
                "INSERT INTO metric_points VALUES (?,?,?,?,?,?,?,?,?,?)", rows
            )
            return MetricExportResult.SUCCESS if ok else MetricExportResult.FAILURE
        except Exception:
            warn("metric_export_failed")
            return MetricExportResult.FAILURE

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        """Report that batches are committed synchronously by export."""
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        """Leave shared store ownership with the provider bundle."""
