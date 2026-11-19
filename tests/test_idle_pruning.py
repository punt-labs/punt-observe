"""Exercise retention during idle capture rather than relying on new exports."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.metrics.export import MetricExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from punt_observe.telemetry import (
    TelemetryConfig,
    _diagnostics,
    open_telemetry,
    providers,
)
from punt_observe.telemetry._pruning import PeriodicPruner
from punt_observe.telemetry.sqlite_exporter import SQLiteStore


@pytest.fixture(autouse=True)
def local_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "PUNT_TELEMETRY",
        "TEST_TELEMETRY",
        "PUNT_TELEMETRY_EXPORTER",
        "PUNT_TELEMETRY_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(providers, "_PRUNE_INTERVAL_S", 0.01)


def pruning_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == "punt-observe-prune"}


def test_idle_retention_prunes_and_reclaims_without_signals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(providers, "_PRUNE_INTERVAL_S", 0.01, raising=False)
    clock = [time.time_ns()]
    monkeypatch.setattr(time, "time_ns", lambda: clock[0])
    path = tmp_path / ".punt-labs/test/telemetry.db"
    seed = SQLiteStore(path, 1, {})
    try:
        for table, timestamp in (
            ("spans", "end_ns"),
            ("span_events", "ts_ns"),
            ("metric_points", "ts_ns"),
        ):
            assert seed.write(
                f"INSERT INTO {table} ({timestamp}, attributes) VALUES (?, ?)",
                [(clock[0], "x" * 8192)] * 64,
            )
    finally:
        seed.close()
    telemetry = open_telemetry("test", [], TelemetryConfig(retention_days=1))
    try:
        with sqlite3.connect(path) as connection:
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            assert connection.execute("SELECT count(*) FROM spans").fetchone()[0] == 64
            clock[0] += 2 * 86_400 * 1_000_000_000
            deadline = time.monotonic() + 2
            rows, free = -1, -1
            while time.monotonic() < deadline:
                rows = sum(
                    connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("spans", "span_events", "metric_points")
                )
                free = connection.execute("PRAGMA freelist_count").fetchone()[0]
                if rows == 0 and free == 0:
                    break
                threading.Event().wait(0.01)
            assert rows == 0, "expired rows remained while telemetry was idle"
            assert free == 0
            assert connection.execute("PRAGMA page_count").fetchone()[0] < pages
    finally:
        telemetry.shutdown()


@pytest.mark.parametrize("disabled", ["config", "global", "tool"])
def test_disabled_capture_never_starts_pruning(
    monkeypatch: pytest.MonkeyPatch,
    disabled: str,
) -> None:
    before = pruning_threads()
    if disabled != "config":
        monkeypatch.setenv(
            "PUNT_TELEMETRY" if disabled == "global" else "TEST_TELEMETRY", "off"
        )
    telemetry = open_telemetry(
        "test", [], TelemetryConfig(enabled=disabled != "config")
    )
    try:
        assert pruning_threads() <= before
    finally:
        telemetry.shutdown()


def test_otlp_has_no_store_or_pruning_thread(tmp_path: Path) -> None:
    before = pruning_threads()
    telemetry = open_telemetry(
        "test",
        [],
        TelemetryConfig(
            exporter="otlp",
            otlp_consent=True,
            otlp_endpoint="https://unused.invalid",
            exporter_factory=lambda _: (
                InMemorySpanExporter(),
                cast(MetricExporter, Mock(spec=MetricExporter)),
            ),
        ),
    )
    try:
        assert pruning_threads() <= before
        assert not (tmp_path / ".punt-labs/test/telemetry.db").exists()
    finally:
        telemetry.shutdown()


def test_shutdown_wakes_hourly_daemon_without_waiting_an_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "_PRUNE_INTERVAL_S", 3600)
    before = pruning_threads()
    telemetry = open_telemetry("test", [])
    workers = pruning_threads() - before
    try:
        assert len(workers) == 1
        assert all(t.daemon for t in workers)
    finally:
        telemetry.shutdown()
    assert all(not t.is_alive() for t in workers)
    telemetry.shutdown()


def test_prune_failure_is_rate_limited_private_and_does_not_stop_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    telemetry = open_telemetry("test", [])
    original = SQLiteStore.prune
    attempts = 0
    recovered = threading.Event()

    def flaky(store: SQLiteStore) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RuntimeError("SECRET_CONTENT")
        original(store)
        recovered.set()

    monkeypatch.setattr(_diagnostics, "_last", 0)
    monkeypatch.setattr(SQLiteStore, "prune", flaky)
    caplog.set_level(logging.WARNING, logger="punt_observe.telemetry")
    try:
        assert recovered.wait(2)
        messages = [
            r.getMessage() for r in caplog.records if r.name == "punt_observe.telemetry"
        ]
        assert messages == ["Telemetry dropped: sqlite_prune_failed"]
        assert "SECRET_CONTENT" not in caplog.text
    finally:
        telemetry.shutdown()


def test_shutdown_wait_is_bounded_even_during_stuck_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = open_telemetry("test", [])
    workers = pruning_threads()
    entered, release = threading.Event(), threading.Event()
    original = SQLiteStore.prune

    def blocked(store: SQLiteStore) -> None:
        entered.set()
        release.wait(5)
        original(store)

    monkeypatch.setattr(SQLiteStore, "prune", blocked)
    try:
        assert entered.wait(2)
        started = time.monotonic()
        telemetry.shutdown(0.02)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), 1e100])
def test_invalid_pruning_interval_starts_no_worker(interval: float) -> None:
    before = pruning_threads()
    with pytest.raises(ValueError):
        PeriodicPruner(None, interval)  # type: ignore[arg-type]
    assert pruning_threads() <= before
