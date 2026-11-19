"""Own explicit private OTel providers without changing application globals."""

from __future__ import annotations

from collections.abc import Callable

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from punt_observe._private_fs import PrivatePaths
from punt_observe.telemetry import otlp
from punt_observe.telemetry._diagnostics import warn
from punt_observe.telemetry.config import TelemetryConfig
from punt_observe.telemetry.sqlite_exporter import (
    SQLiteMetricExporter,
    SQLiteSpanExporter,
    SQLiteStore,
)

type ExporterFactory = Callable[[str], tuple[SpanExporter, MetricExporter]]


class Providers:
    """Own private resource, exporters, workers and their shared storage."""

    def __init__(
        self, tool: str, config: TelemetryConfig,
        exporter_factory: ExporterFactory | None = None,
    ) -> None:
        self.store: SQLiteStore | None = None
        resource_attrs: dict[str, str | int] = {"service.name": tool, "punt.schema": 1}
        resource = Resource(resource_attrs)
        selected: tuple[SpanExporter, MetricExporter] | None = None
        if config.exporter == "otlp" and config.otlp_endpoint:
            try:
                selected = (exporter_factory or otlp.exporters)(config.otlp_endpoint)
            except ImportError:
                warn("otlp_extra_missing")
        if selected is None:
            self.store = SQLiteStore(
                PrivatePaths.root(tool) / "telemetry.db", config.retention_days,
                resource_attrs,
            )
            selected = SQLiteSpanExporter(self.store), SQLiteMetricExporter(self.store)
        self.traces = TracerProvider(resource=resource, shutdown_on_exit=False)
        self.processor = BatchSpanProcessor(
            selected[0], max_queue_size=2048, max_export_batch_size=512,
            schedule_delay_millis=1000, export_timeout_millis=2000,
        )
        self.traces.add_span_processor(self.processor)
        self.reader = PeriodicExportingMetricReader(
            selected[1], export_interval_millis=1000, export_timeout_millis=2000
        )
        self.metrics = MeterProvider(
            resource=resource, metric_readers=[self.reader], shutdown_on_exit=False
        )
        self.tracer = self.traces.get_tracer("punt_observe", "1")
        self.meter = self.metrics.get_meter("punt_observe", "1")

    def shutdown(self) -> None:
        """Flush and stop providers, closing storage even if an exporter fails."""
        try:
            self.traces.shutdown()
        finally:
            try:
                self.metrics.shutdown(timeout_millis=2000)
            finally:
                if self.store is not None:
                    self.store.close()
