"""Lazily construct consented OTLP/HTTP exporters, never auto-detect settings."""

from __future__ import annotations

import importlib
from typing import cast

from opentelemetry.sdk.metrics.export import MetricExporter
from opentelemetry.sdk.trace.export import SpanExporter


def exporters(endpoint: str) -> tuple[SpanExporter, MetricExporter]:
    """Create bounded HTTP exporters from the optional otlp extra.

    Raises:
        ImportError: The optional exporter dependency is unavailable.
    """
    traces = importlib.import_module(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    )
    metrics = importlib.import_module(
        "opentelemetry.exporter.otlp.proto.http.metric_exporter"
    )
    return (
        cast(
            SpanExporter,
            traces.OTLPSpanExporter(
                endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=2
            ),
        ),
        cast(
            MetricExporter,
            metrics.OTLPMetricExporter(
                endpoint=endpoint.rstrip("/") + "/v1/metrics", timeout=2
            ),
        ),
    )
