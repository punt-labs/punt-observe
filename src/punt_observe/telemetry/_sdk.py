"""Keep optional OpenTelemetry SDK self-observation out of global providers."""

from __future__ import annotations

from collections.abc import Callable
from inspect import signature

from opentelemetry.metrics import NoOpMeterProvider

_NOOP_METER_PROVIDER = NoOpMeterProvider()


def private_meter_options(
    factory: Callable[..., object],
) -> dict[str, NoOpMeterProvider]:
    """Disable SDK self-metrics where the installed version supports them."""
    if "meter_provider" in signature(factory).parameters:
        return {"meter_provider": _NOOP_METER_PROVIDER}
    return {}
