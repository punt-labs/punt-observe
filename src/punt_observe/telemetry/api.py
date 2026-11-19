"""Expose failure-isolated instrumentation validated before private OTel calls."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry.trace import Span, Status, StatusCode

from punt_observe.telemetry._diagnostics import warn
from punt_observe.telemetry.config import TelemetryConfig, resolve
from punt_observe.telemetry.providers import ExporterFactory, Providers
from punt_observe.telemetry.salt import hash_identifier
from punt_observe.telemetry.schema import (
    IdentifierHash,
    SchemaRegistry,
    SignalSchema,
    Value,
)


class Telemetry:
    """Observe closed-schema signals without changing the caller's result."""

    def __init__(
        self, tool: str, schemas: Sequence[SignalSchema],
        config: TelemetryConfig = TelemetryConfig(), *,
        exporter_factory: ExporterFactory | None = None,
    ) -> None:
        self._providers: Providers | None = None
        self._registry = SchemaRegistry(list(schemas))
        self._tool = tool
        self._current: ContextVar[Span | None] = ContextVar("punt_span", default=None)
        self._instruments: dict[tuple[str, int], Any] = {}
        self._lock = threading.Lock()
        self._stopped = False
        try:
            effective = resolve(tool, config)
            if effective.enabled:
                self._providers = Providers(tool, effective, exporter_factory)
        except Exception:
            warn("initialization_failed")

    def _validate(
        self, name: str, version: int, attrs: Mapping[str, object], kind: str
    ) -> dict[str, Value] | None:
        if self._providers is None or self._stopped:
            return None
        try:
            schema = self._registry.get(name, version)
            if schema is None or schema.kind != kind or not name.startswith(self._tool + "."):
                warn("unknown_schema_or_kind")
                return None
            validated = self._registry.validate(name, version, attrs)
            if isinstance(validated, str):
                warn(validated)
                return None
            validated["punt.schema_version"] = version
            return validated
        except Exception:
            warn("validation_failed")
            return None

    @contextmanager
    def span(self, name: str, version: int, **attrs: object) -> Iterator[None]:
        """Observe a span without capturing exception text or suppressing errors.

        Exceptions from the caller's context propagate unchanged; instrumentation
        failures never do. An optional declared outcome enum receives ok/error.
        """
        active: Span | None = None
        token = None
        try:
            validated = self._validate(name, version, attrs, "span")
            if validated is not None and self._providers is not None:
                active = self._providers.tracer.start_span(name, attributes=validated)
                token = self._current.set(active)
        except Exception:
            warn("span_start_failed")
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            if active is not None:
                try:
                    outcome = "error" if failed else "ok"
                    check = self._registry.validate(name, version, {"outcome": outcome})
                    if isinstance(check, dict):
                        active.set_attribute("outcome", outcome)
                    active.set_status(Status(StatusCode.ERROR if failed else StatusCode.OK))
                    active.end()
                except Exception:
                    warn("span_end_failed")
            if token is not None:
                self._current.reset(token)

    def event(self, name: str, version: int, **attrs: object) -> None:
        """Observe a validated event on the private current span or its own span."""
        try:
            validated = self._validate(name, version, attrs, "event")
            if validated is None or self._providers is None:
                return
            current = self._current.get()
            if current is not None:
                current.add_event(name, validated)
            else:
                validated["punt.signal"] = "event"
                now = time.time_ns()
                span = self._providers.tracer.start_span(
                    name, attributes=validated, start_time=now
                )
                span.end(end_time=now)
        except Exception:
            warn("event_failed")

    def _metric(
        self, name: str, version: int, value: float, attrs: Mapping[str, object],
        kind: str,
    ) -> None:
        try:
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                warn("invalid_measurement")
                return
            validated = self._validate(name, version, attrs, kind)
            if validated is None or self._providers is None:
                return
            schema = self._registry.get(name, version)
            if schema is None:
                return
            key = (name, version)
            with self._lock:
                instrument = self._instruments.get(key)
                if instrument is None:
                    create = (
                        self._providers.meter.create_counter if kind == "counter"
                        else self._providers.meter.create_histogram
                    )
                    instrument = create(name, unit=schema.unit)
                    self._instruments[key] = instrument
            if kind == "counter":
                instrument.add(value, validated)
            else:
                instrument.record(value, validated)
        except Exception:
            warn("metric_failed")

    def count(self, name: str, version: int, n: int = 1, **attrs: object) -> None:
        """Add a nonnegative count after validating its declared attributes."""
        if type(n) is not int:
            warn("invalid_count")
            return
        self._metric(name, version, n, attrs, "counter")

    def record(self, name: str, version: int, value: float, **attrs: object) -> None:
        """Record a finite nonnegative histogram value in schema-declared units."""
        self._metric(name, version, value, attrs, "histogram")

    def hash_id(self, value: str) -> IdentifierHash:
        """Derive a salted identifier, returning an invalid sentinel on failure."""
        if self._providers is None or self._stopped:
            return IdentifierHash("")
        try:
            return hash_identifier(value)
        except Exception:
            warn("identifier_failed")
            return IdentifierHash("")

    @staticmethod
    def length(text: str) -> int:
        """Return content length without retaining or exporting the content."""
        try:
            return len(text)
        except Exception:
            return 0

    def shutdown(self, timeout_s: float = 2.0) -> None:
        """Stop capture and bound waiting for private-provider flushing."""
        if self._stopped:
            return
        self._stopped = True
        if self._providers is None:
            return
        providers = self._providers

        def finish() -> None:
            try:
                providers.shutdown()
            except Exception:
                warn("shutdown_failed")

        try:
            worker = threading.Thread(target=finish, name="punt-observe-shutdown", daemon=True)
            worker.start()
            worker.join(timeout=max(0, min(timeout_s, 30)))
        except Exception:
            warn("shutdown_failed")


class NoopTelemetry(Telemetry):
    """Provide the facade without creating stores, providers or workers."""


def open_telemetry(
    tool: str, schemas: Sequence[SignalSchema] = (),
    config: TelemetryConfig = TelemetryConfig(), *,
    exporter_factory: ExporterFactory | None = None,
) -> Telemetry:
    """Open failure-isolated capture, or a resource-free disabled facade."""
    try:
        effective = resolve(tool, config)
    except Exception:
        effective = TelemetryConfig(enabled=False)
    cls = Telemetry if effective.enabled else NoopTelemetry
    return cls(tool, schemas, effective, exporter_factory=exporter_factory)
