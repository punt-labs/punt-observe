"""Own explicit private OTel providers without changing application globals."""

from __future__ import annotations

import os
import weakref
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any, cast

from opentelemetry.sdk.metrics import (
    AlwaysOffExemplarFilter,
    Counter,
    Histogram,
    MeterProvider,
)
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricExporter,
    MetricExportResult,
    MetricsData,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from punt_observe._private_fs import PrivatePaths
from punt_observe.telemetry import otlp
from punt_observe.telemetry._diagnostics import warn
from punt_observe.telemetry._pruning import PeriodicPruner
from punt_observe.telemetry.config import TelemetryConfig
from punt_observe.telemetry.sqlite_exporter import (
    SQLiteMetricExporter,
    SQLiteSpanExporter,
    SQLiteStore,
)

type ExporterFactory = Callable[[str], tuple[SpanExporter, MetricExporter]]

_PRUNE_INTERVAL_S = 3600.0


def _cleanup_failed_setup(shutdown: Callable[[], None]) -> None:
    try:
        shutdown()
    except BaseException:
        warn("setup_cleanup_failed")


class _PrivateResource(Resource):
    def merge(self, other: Resource) -> Resource:
        """Keep the fixed allowlist intact across SDK post-fork refreshes."""
        return self


class _DeltaMetricExporter(MetricExporter):
    """Request deltas consistently for local, network and injected exporters."""

    def __init__(self, exporter: MetricExporter) -> None:
        super().__init__(
            preferred_temporality={
                Counter: AggregationTemporality.DELTA,
                Histogram: AggregationTemporality.DELTA,
            }
        )
        self._exporter = exporter

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: Any,
    ) -> MetricExportResult:
        """Forward delta batches with the reader's export deadline."""
        return cast(
            MetricExportResult,
            cast(Any, self._exporter).export(
                metrics_data, timeout_millis=timeout_millis, **kwargs
            ),
        )

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        """Flush the selected exporter within the provided deadline."""
        return self._exporter.force_flush(timeout_millis=timeout_millis)

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        """Stop the selected exporter without changing its lifecycle ownership."""
        cast(Any, self._exporter).shutdown(timeout_millis=timeout_millis, **kwargs)


class Providers:
    """Own private resource, exporters, workers and their shared storage."""

    def __init__(
        self,
        tool: str,
        config: TelemetryConfig,
        exporter_factory: ExporterFactory | None = None,
    ) -> None:
        self.store: SQLiteStore | None = None
        self._pruner: PeriodicPruner | None = None
        self._pruning_stopped = False
        with ExitStack() as cleanup:
            resource_attrs: dict[str, str | int] = {
                "service.name": tool,
                "punt.schema": 1,
            }
            resource = _PrivateResource(resource_attrs)
            selected: tuple[SpanExporter, MetricExporter] | None = None
            if config.exporter == "otlp" and config.otlp_endpoint:
                try:
                    selected = (
                        exporter_factory or config.exporter_factory or otlp.exporters
                    )(config.otlp_endpoint)
                except ImportError:
                    warn("otlp_extra_missing")
            if selected is None:
                self.store = SQLiteStore(
                    PrivatePaths.root(tool) / "telemetry.db",
                    config.retention_days,
                    resource_attrs,
                )
                cleanup.callback(_cleanup_failed_setup, self.store.close)
                selected = (
                    SQLiteSpanExporter(self.store),
                    SQLiteMetricExporter(self.store),
                )
            span_cleanup = cleanup.enter_context(ExitStack())
            span_cleanup.callback(_cleanup_failed_setup, selected[0].shutdown)
            metric_cleanup = cleanup.enter_context(ExitStack())
            metric_cleanup.callback(
                _cleanup_failed_setup, cast(Any, selected[1]).shutdown
            )
            self.traces = TracerProvider(resource=resource, shutdown_on_exit=False)
            self.processor = BatchSpanProcessor(
                selected[0],
                max_queue_size=2048,
                max_export_batch_size=512,
                schedule_delay_millis=1000,
                export_timeout_millis=2000,
            )
            span_cleanup.pop_all()
            span_cleanup.callback(_cleanup_failed_setup, self.processor.shutdown)
            self.traces.add_span_processor(self.processor)
            span_cleanup.pop_all()
            span_cleanup.callback(_cleanup_failed_setup, self.traces.shutdown)
            self.reader = PeriodicExportingMetricReader(
                _DeltaMetricExporter(selected[1]),
                export_interval_millis=60_000,
                export_timeout_millis=2000,
            )
            metric_cleanup.pop_all()
            metric_cleanup.callback(
                _cleanup_failed_setup, cast(Any, self.reader).shutdown
            )
            self.metrics = MeterProvider(
                resource=resource,
                metric_readers=[self.reader],
                shutdown_on_exit=False,
                exemplar_filter=AlwaysOffExemplarFilter(),
            )
            metric_cleanup.pop_all()
            metric_cleanup.callback(_cleanup_failed_setup, self.metrics.shutdown)
            self.tracer = self.traces.get_tracer("punt_observe", "1")
            self.meter = self.metrics.get_meter("punt_observe", "1")
            if self.store is not None:
                self._pruner = PeriodicPruner(self.store, _PRUNE_INTERVAL_S)
                cleanup.callback(_cleanup_failed_setup, self._pruner.join)
                cleanup.callback(self.stop_pruning)
                reference = weakref.ref(self)

                def after_fork() -> None:
                    bundle = reference()
                    if bundle is not None:
                        bundle._restart_pruning()

                os.register_at_fork(after_in_child=after_fork)
            cleanup.pop_all()

    def _restart_pruning(self) -> None:
        self._pruner = None
        if self.store is not None and not self._pruning_stopped:
            try:
                self.store.reset_after_fork()
                self._pruner = PeriodicPruner(self.store, _PRUNE_INTERVAL_S)
            except Exception:
                warn("sqlite_prune_failed")

    def stop_pruning(self) -> None:
        """Cancel scheduled retention without blocking provider shutdown."""
        self._pruning_stopped = True
        if self._pruner is not None:
            self._pruner.stop()

    def shutdown(self) -> None:
        """Flush and stop providers, closing storage even if an exporter fails."""
        self.stop_pruning()
        try:
            self.traces.shutdown()
        finally:
            try:
                self.metrics.shutdown(timeout_millis=2000)
            finally:
                if self._pruner is not None:
                    self._pruner.join()
                if self.store is not None:
                    self.store.close()
