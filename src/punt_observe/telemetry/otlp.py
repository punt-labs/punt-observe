"""Lazily construct consented OTLP/HTTP exporters, never auto-detect settings."""

from __future__ import annotations

import importlib
from typing import Any, cast

from opentelemetry.sdk.metrics import Counter, Histogram
from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricExporter
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation
from opentelemetry.sdk.trace.export import SpanExporter


class _PrivateSession:
    def __init__(self) -> None:
        requests = importlib.import_module("requests")
        self._session = requests.Session()
        self._session.trust_env = False
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Pin wire settings even when SDK versions merge ambient configuration."""
        timeout = kwargs.get("timeout")
        kwargs.update(
            headers={"Content-Type": "application/x-protobuf"},
            verify=True,
            cert=None,
            allow_redirects=False,
            timeout=2 if timeout is None else min(timeout, 2),
        )
        response = self._session.request(method, url, **kwargs)
        if 300 <= response.status_code < 400:
            response.status_code = 400
            response.reason = "OTLP redirect refused"
            response._content = b""
        return response

    def post(self, url: str, **kwargs: Any) -> Any:
        """Support SDK versions that send directly through Session.post."""
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        """Close only this exporter's isolated connection pool."""
        self._session.close()


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
    compression = importlib.import_module("opentelemetry.exporter.otlp.proto.http")
    options: dict[str, Any] = {
        "timeout": 2,
        "headers": {},
        "compression": compression.Compression.NoCompression,
    }
    return (
        cast(
            SpanExporter,
            traces.OTLPSpanExporter(
                endpoint=endpoint.rstrip("/") + "/v1/traces",
                session=_PrivateSession(),
                **options,
            ),
        ),
        cast(
            MetricExporter,
            metrics.OTLPMetricExporter(
                endpoint=endpoint.rstrip("/") + "/v1/metrics",
                session=_PrivateSession(),
                **options,
                preferred_temporality={
                    Counter: AggregationTemporality.DELTA,
                    Histogram: AggregationTemporality.DELTA,
                },
                preferred_aggregation={Histogram: ExplicitBucketHistogramAggregation()},
            ),
        ),
    )
