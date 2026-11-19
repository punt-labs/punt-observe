"""Resolve capture controls without making network export implicit."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Literal

from punt_observe._private_fs import PrivatePaths


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """Configure bounded retention and explicit network-export consent.

    Raises:
        ValueError: Retention is unlimited, out of bounds or exporter unknown.
    """

    enabled: bool = True
    retention_days: int = 90
    exporter: Literal["sqlite", "otlp"] = "sqlite"
    otlp_endpoint: str | None = None
    otlp_consent: bool = False

    def __post_init__(self) -> None:
        if type(self.retention_days) is not int or not 1 <= self.retention_days <= 3650:
            raise ValueError("retention must be 1..3650 days")
        if self.exporter not in ("sqlite", "otlp"):
            raise ValueError("unknown exporter")


def resolve(tool: str, config: TelemetryConfig) -> TelemetryConfig:
    """Apply any-off-wins controls and require config-only network consent."""
    PrivatePaths.root(tool)
    off = {"off", "0", "false", "no"}
    enabled = config.enabled and not any(
        os.environ.get(key, "").strip().lower() in off
        for key in ("PUNT_TELEMETRY", f"{tool.upper().replace('-', '_')}_TELEMETRY")
    )
    endpoint = os.environ.get("PUNT_TELEMETRY_OTLP_ENDPOINT", config.otlp_endpoint)
    selected = os.environ.get("PUNT_TELEMETRY_EXPORTER", config.exporter)
    exporter: Literal["sqlite", "otlp"] = (
        "otlp" if selected == "otlp" and config.otlp_consent and endpoint else "sqlite"
    )
    return replace(config, enabled=enabled, exporter=exporter, otlp_endpoint=endpoint)
