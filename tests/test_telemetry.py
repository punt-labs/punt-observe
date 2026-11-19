"""Verify closed schemas, private OTel storage, controls and failure isolation."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import sqlite3
import stat
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from punt_observe.telemetry import (
    BoolAttr,
    CountAttr,
    DurationAttr,
    EnumAttr,
    HashAttr,
    IntAttr,
    LengthAttr,
    NoopTelemetry,
    SchemaRegistry,
    SignalSchema,
    SizeAttr,
    TelemetryConfig,
    open_telemetry,
)
from punt_observe.telemetry.config import resolve
from punt_observe.telemetry.salt import hash_identifier, load_salt
from punt_observe.telemetry.sqlite_exporter import SQLiteStore


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "PUNT_TELEMETRY",
        "TEST_TELEMETRY",
        "PUNT_TELEMETRY_EXPORTER",
        "PUNT_TELEMETRY_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)


SCHEMAS = [
    SignalSchema(
        "test.cli.run",
        1,
        "span",
        {"outcome": EnumAttr(("ok", "error")), "id": HashAttr()},
    ),
    SignalSchema(
        "test.cli.used", 1, "event", {"size": SizeAttr(), "enabled": BoolAttr()}
    ),
    SignalSchema("test.cli.calls", 1, "counter", {"count": CountAttr()}),
    SignalSchema("test.cli.duration", 1, "histogram", {}, unit="ms"),
]


class CapturingMetricExporter(MetricExporter):
    """Capture metric batches without network or disk IO."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[MetricsData] = []

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: object,
    ) -> MetricExportResult:
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        pass


@pytest.mark.parametrize(
    "attrs",
    [
        {},
        {"n": 2, "b": True, "e": "ok", "d": 1.5, "l": 0},
    ],
)
def test_schema_valid(attrs: dict[str, object]) -> None:
    if attrs:
        attrs = {**attrs, "h": hash_identifier("known-id")}
    registry = SchemaRegistry(
        [
            SignalSchema(
                "test.cli.run",
                1,
                "span",
                {
                    "n": IntAttr(min=0, max=3),
                    "b": BoolAttr(),
                    "e": EnumAttr(("ok",)),
                    "d": DurationAttr(),
                    "l": LengthAttr(),
                    "h": HashAttr(),
                },
            )
        ]
    )
    assert isinstance(registry.validate("test.cli.run", 1, attrs), dict)


@pytest.mark.parametrize(
    "name,version,attrs",
    [
        ("unknown", 1, {}),
        ("test.cli.run", 2, {}),
        ("test.cli.run", True, {}),
        ("test.cli.run", 1, {"unknown": 1}),
        ("test.cli.run", 1, {"n": True}),
        ("test.cli.run", 1, {"n": -1}),
        ("test.cli.run", 1, {"e": "free text"}),
        ("test.cli.run", 1, {"h": "a" * 32}),
        ("test.cli.run", 1, {"b": 1}),
    ],
)
def test_schema_rejection(name: str, version: int, attrs: dict[str, object]) -> None:
    registry = SchemaRegistry(
        [
            SignalSchema(
                "test.cli.run",
                1,
                "span",
                {
                    "n": IntAttr(min=0),
                    "e": EnumAttr(("ok",)),
                    "h": HashAttr(),
                    "b": BoolAttr(),
                },
            )
        ]
    )
    assert isinstance(registry.validate(name, version, attrs), str)


@pytest.mark.parametrize("days", [0, -1, 3651, None, float("inf")])
def test_retention_invalid(days: object) -> None:
    with pytest.raises(ValueError):
        TelemetryConfig(**{"retention_days": days})  # type: ignore[arg-type]


@pytest.mark.parametrize("env", ["PUNT_TELEMETRY", "TEST_TELEMETRY"])
@pytest.mark.parametrize("value", ["off", "FALSE", "0", "No"])
def test_off_switch(
    env: str, value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(env, value)
    before = set(threading.enumerate())
    telemetry = open_telemetry("test", SCHEMAS)
    telemetry.event("test.cli.used", 1, size=1)
    with telemetry.span("test.cli.run", 1):
        pass
    telemetry.hash_id("raw")
    telemetry.shutdown()
    assert isinstance(telemetry, NoopTelemetry)
    assert set(threading.enumerate()) == before
    assert not (tmp_path / ".punt-labs").exists()


def test_config_disabled_and_consent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    telemetry = open_telemetry("test", SCHEMAS, TelemetryConfig(enabled=False))
    assert isinstance(telemetry, NoopTelemetry)
    assert not (tmp_path / ".punt-labs").exists()
    monkeypatch.setenv("PUNT_TELEMETRY", "on")
    monkeypatch.setenv("TEST_TELEMETRY", "off")
    assert not resolve("test", TelemetryConfig()).enabled
    monkeypatch.setenv("PUNT_TELEMETRY_EXPORTER", "otlp")
    monkeypatch.setenv("PUNT_TELEMETRY_OTLP_ENDPOINT", "http://localhost:4318")
    assert resolve("test", TelemetryConfig()).exporter == "sqlite"


def test_sqlite_all_signals_and_privacy(tmp_path: Path) -> None:
    global_trace, global_meter = (
        trace.get_tracer_provider(),
        metrics.get_meter_provider(),
    )
    telemetry = open_telemetry("test", SCHEMAS)
    hashed = telemetry.hash_id("user-raw-content")
    assert len(hashed.value) == 32
    assert telemetry.hash_id("user-raw-content") == hashed
    assert (
        stat.S_IMODE((tmp_path / ".punt-labs/telemetry.salt").stat().st_mode) == 0o600
    )
    with telemetry.span("test.cli.run", 1, id=hashed):
        telemetry.event("test.cli.used", 1, size=3, enabled=True)
    with pytest.raises(ValueError, match="PRIVATE"), telemetry.span("test.cli.run", 1):
        raise ValueError("PRIVATE")
    telemetry.event("test.cli.used", 1)
    telemetry.event("test.cli.used", 1, unknown="DO-NOT-STORE")
    telemetry.event("unknown-secret-name", 1, size=3)
    telemetry.count("test.cli.calls", 1, count=2)
    telemetry.record("test.cli.duration", 1, 1.25)
    telemetry.shutdown(5)
    assert trace.get_tracer_provider() is global_trace
    assert metrics.get_meter_provider() is global_meter
    path = tmp_path / ".punt-labs/test/telemetry.db"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    rows = conn.execute("SELECT name,status,attributes FROM spans").fetchall()
    assert len(rows) == 3
    assert any(json.loads(r[2]).get("outcome") == "error" for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM span_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0] >= 2
    conn.close()
    raw = path.read_bytes()
    assert b"PRIVATE" not in raw and b"DO-NOT-STORE" not in raw
    assert b"user-raw-content" not in raw and load_salt() not in raw


def test_sidecars_and_retention(tmp_path: Path) -> None:
    path = tmp_path / "data/telemetry.db"
    store = SQLiteStore(path, 1, {"service.name": "test"})
    assert store.write(
        "INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("t", str(i), None, "test.cli.run", 1, "span", 0, 0, "OK", "x" * 4096)
            for i in range(200)
        ],
    )
    for sidecar in path.parent.iterdir():
        assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    observer = sqlite3.connect(path)
    pages_before = observer.execute("PRAGMA page_count").fetchone()[0]
    store.prune()
    assert observer.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 0
    assert observer.execute("PRAGMA page_count").fetchone()[0] < pages_before
    assert observer.execute("PRAGMA freelist_count").fetchone()[0] < pages_before
    observer.close()
    store.close()


def test_failures_never_change_result(tmp_path: Path) -> None:
    with patch(
        "punt_observe.telemetry.providers.SQLiteStore", side_effect=PermissionError
    ):
        telemetry = open_telemetry("test", SCHEMAS)
        with telemetry.span("test.cli.run", 1):
            result = 42
        telemetry.event("test.cli.used", 1)
        telemetry.shutdown()
        assert result == 42
    telemetry = open_telemetry("test", SCHEMAS)
    assert telemetry._providers is not None
    with patch.object(
        telemetry._providers.processor.span_exporter, "export", side_effect=OSError
    ):
        telemetry.event("test.cli.used", 1)
        assert telemetry._providers.traces.force_flush(timeout_millis=2000)
    telemetry.shutdown()
    store = SQLiteStore(tmp_path / "full.db", 90, {})
    assert not store.write("INSERT INTO missing VALUES (?)", [(1,)])
    store.close()


def test_exporter_swap_and_bounded_shutdown(tmp_path: Path) -> None:
    captured = InMemorySpanExporter()
    exporter = CapturingMetricExporter()
    telemetry = open_telemetry(
        "test",
        SCHEMAS,
        TelemetryConfig(
            exporter="otlp",
            otlp_endpoint="http://unused",
            otlp_consent=True,
            exporter_factory=lambda _: (captured, exporter),
        ),
    )
    with telemetry.span("test.cli.run", 1):
        telemetry.event("test.cli.used", 1)
    telemetry.count("test.cli.calls", 1)
    telemetry.shutdown(5)
    assert len(captured.get_finished_spans()) == 1
    assert exporter.batches
    assert not (tmp_path / ".punt-labs/test/telemetry.db").exists()
    telemetry = open_telemetry("test", SCHEMAS)
    assert telemetry._providers is not None
    done = threading.Event()
    with patch.object(
        telemetry._providers, "shutdown", side_effect=lambda: done.wait(2)
    ):
        started = time.monotonic()
        telemetry.shutdown(0.01)
        assert time.monotonic() - started < 0.5
        done.set()
    telemetry._providers.shutdown()


def test_bounded_queue_drops_on_saturation() -> None:
    gate = threading.Event()
    entered = threading.Event()
    telemetry = open_telemetry("test", SCHEMAS)
    assert telemetry._providers is not None
    exporter = telemetry._providers.processor.span_exporter
    exported: list[int] = []

    def blocked(spans: Sequence[ReadableSpan]) -> None:
        entered.set()
        gate.wait(5)
        exported.append(len(spans))

    with patch.object(exporter, "export", side_effect=blocked):
        for _ in range(512):
            telemetry.event("test.cli.used", 1)
        assert entered.wait(3)
        started = time.monotonic()
        for _ in range(3000):
            telemetry.event("test.cli.used", 1)
        assert time.monotonic() - started < 2
        gate.set()
        telemetry.shutdown(5)
    assert 0 < sum(exported) <= 512 + 2048


def test_salt_races_and_tightening(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(load_salt) for _ in range(16)]
        salts = {future.result() for future in futures}
    assert len(salts) == 1
    assert len(salts.pop()) == 32
    root = tmp_path / ".punt-labs"
    assert not list(root.glob(".salt-*"))
    path = root / "telemetry.salt"
    path.chmod(0o644)
    load_salt()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_bytes(b"short")
    with pytest.raises(ValueError):
        load_salt()
    path.unlink()
    path.symlink_to(tmp_path / "victim")
    with pytest.raises(OSError):
        load_salt()


@pytest.mark.parametrize("config_off", [False, True])
@pytest.mark.parametrize("global_off", [False, True])
@pytest.mark.parametrize("tool_off", [False, True])
def test_any_off_wins(
    config_off: bool,
    global_off: bool,
    tool_off: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUNT_TELEMETRY", "off" if global_off else "on")
    monkeypatch.setenv("MY_TOOL_TELEMETRY", "off" if tool_off else "on")
    assert resolve("my-tool", TelemetryConfig(enabled=not config_off)).enabled == (
        not (config_off or global_off or tool_off)
    )


def test_otlp_without_consent_and_missing_extra(tmp_path: Path) -> None:
    config = TelemetryConfig(exporter="otlp", otlp_endpoint="http://unused")
    telemetry = open_telemetry("test", SCHEMAS, config)
    telemetry.event("test.cli.used", 1)
    telemetry.shutdown(5)
    assert (tmp_path / ".punt-labs/test/telemetry.db").exists()
    with patch("punt_observe.telemetry.otlp.exporters", side_effect=ImportError):
        telemetry = open_telemetry(
            "test",
            SCHEMAS,
            TelemetryConfig(
                exporter="otlp", otlp_endpoint="http://unused", otlp_consent=True
            ),
        )
        with telemetry.span("test.cli.run", 1):
            pass
        telemetry.shutdown(5)
    conn = sqlite3.connect(tmp_path / ".punt-labs/test/telemetry.db")
    assert conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 2
    conn.close()


def test_private_parent_context_and_no_exception_recording() -> None:
    captured = InMemorySpanExporter()
    telemetry = open_telemetry(
        "test",
        SCHEMAS,
        TelemetryConfig(
            exporter="otlp",
            otlp_endpoint="http://unused",
            otlp_consent=True,
            exporter_factory=lambda _: (captured, CapturingMetricExporter()),
        ),
    )
    foreign = trace.NonRecordingSpan(trace.SpanContext(1, 2, False))
    with (
        trace.use_span(foreign),
        telemetry.span("test.cli.run", 1),
        telemetry.span("test.cli.run", 1),
    ):
        telemetry.event("test.cli.used", 1)
    telemetry.shutdown(5)
    child, parent = captured.get_finished_spans()
    assert parent.parent is None
    assert parent.context is not None
    assert child.parent is not None
    assert child.parent.span_id == parent.context.span_id
    assert len(child.events) == 1


def test_invalid_inputs_drop_and_initialization_never_raises(tmp_path: Path) -> None:
    duplicate = open_telemetry("test", [SCHEMAS[0], SCHEMAS[0]])
    duplicate.event("test.cli.used", 1)
    duplicate.shutdown()
    assert not (tmp_path / ".punt-labs").exists()
    telemetry = open_telemetry("test", SCHEMAS)
    telemetry.count("test.cli.calls", 1, n=True)
    telemetry.count("test.cli.calls", 1, n=-1)
    telemetry.record("test.cli.duration", 1, float("nan"))
    telemetry.event("test.cli.used", 9)
    with telemetry.span("unknown", 1):
        result = "unchanged"
    telemetry.shutdown(5)
    assert result == "unchanged"
    conn = sqlite3.connect(tmp_path / ".punt-labs/test/telemetry.db")
    assert conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0] == 0
    conn.close()
