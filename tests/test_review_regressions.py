"""Exercise privacy, durability and idle-export review regressions."""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import SupportsIndex
from unittest.mock import patch

import pytest
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from punt_observe.logging import LoggingSetupError, configure_logging
from punt_observe.logging.redactor import LogRedactor
from punt_observe.telemetry import (
    CountAttr,
    HashAttr,
    IdentifierHash,
    LengthAttr,
    SchemaRegistry,
    SignalSchema,
    SizeAttr,
    TelemetryConfig,
    open_telemetry,
)
from punt_observe.telemetry.salt import hash_identifier, load_salt
from punt_observe.telemetry.sqlite_exporter import SQLiteSpanExporter, SQLiteStore
from tests.test_telemetry import SCHEMAS, CapturingMetricExporter

# pyright: reportPrivateUsage=false


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("PUNT_TELEMETRY", "TEST_TELEMETRY", "PUNT_TELEMETRY_EXPORTER"):
        monkeypatch.delenv(key, raising=False)
    root = logging.getLogger()
    handlers, level, fallback = root.handlers[:], root.level, logging.lastResort
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers = handlers
    root.setLevel(level)
    logging.lastResort = fallback


def _tree(home: Path) -> list[Path]:
    root = home / ".punt-labs"
    levels = [root, root / "test", root / "test/logs"]
    for path in levels:
        path.mkdir(exist_ok=True)
        path.chmod(0o755)
    return levels


def test_all_owned_directory_levels_are_private(tmp_path: Path) -> None:
    levels = _tree(tmp_path)
    configure_logging("test", "cli")
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in levels)
    for path in levels:
        path.chmod(0o755)
    telemetry = open_telemetry("test", SCHEMAS)
    try:
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 for p in levels[:2])
    finally:
        telemetry.shutdown(5)


@pytest.mark.parametrize("level", [0, 1, 2])
def test_logging_refuses_symlink_directory_levels(tmp_path: Path, level: int) -> None:
    path = _tree(tmp_path)[level]
    real = path.with_name(path.name + "-real")
    path.rename(real)
    path.symlink_to(real, target_is_directory=True)
    with pytest.raises(LoggingSetupError):
        configure_logging("test", "cli")


@pytest.mark.parametrize("level", [0, 1, 2])
def test_directory_ownership_is_required(tmp_path: Path, level: int) -> None:
    target = _tree(tmp_path)[level].stat()
    original = os.fstat

    def foreign_owner(fd: int) -> os.stat_result:
        info = original(fd)
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            values = list(info)
            values[4] = info.st_uid + 1
            return os.stat_result(values)
        return info

    with patch("os.fstat", side_effect=foreign_owner):
        with pytest.raises(LoggingSetupError):
            configure_logging("test", "cli")
        if level < 2:
            telemetry = open_telemetry("test", SCHEMAS)
            try:
                assert telemetry._providers is None
            finally:
                telemetry.shutdown(5)


@pytest.mark.parametrize("level", [0, 1])
def test_telemetry_refuses_symlink_directory_levels(tmp_path: Path, level: int) -> None:
    path = _tree(tmp_path)[level]
    real = path.with_name(path.name + "-real")
    path.rename(real)
    path.symlink_to(real, target_is_directory=True)
    telemetry = open_telemetry("test", SCHEMAS)
    try:
        assert telemetry._providers is None
    finally:
        telemetry.shutdown(5)


def test_directly_wrapped_hash_is_rejected() -> None:
    registry = SchemaRegistry(
        [SignalSchema("test.cli.run", 1, "span", {"id": HashAttr()})]
    )
    assert isinstance(
        registry.validate("test.cli.run", 1, {"id": IdentifierHash("a" * 32)}), str
    )
    assert isinstance(
        registry.validate("test.cli.run", 1, {"id": hash_identifier("a" * 32)}), dict
    )


def test_last_resort_redacts_handlerless_logger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("test", "cli", stderr_level="ERROR")
    logger = logging.getLogger("review.handlerless")
    previous = logger.propagate, logger.level, logger.handlers[:]
    logger.propagate, logger.handlers = False, []
    logger.setLevel(logging.DEBUG)
    try:
        logger.warning("fetch %s", "https://u:SECRET@h/p?q=SECRET")
        output = capsys.readouterr().err
        assert "SECRET" not in output
        assert "https://h/" in output
        assert logging.lastResort is not None
        assert logging.lastResort.level == logging.WARNING
    finally:
        logger.propagate, logger.level, logger.handlers = previous


def test_replacing_a_derived_hash_cannot_attest_raw_content() -> None:
    registry = SchemaRegistry(
        [SignalSchema("test.cli.run", 1, "span", {"id": HashAttr()})]
    )
    forged = replace(hash_identifier("known-id"), value="a" * 32)
    assert isinstance(registry.validate("test.cli.run", 1, {"id": forged}), str)


def test_discarded_suffix_is_never_scanned_in_full() -> None:
    class GuardedText(str):
        def __getitem__(
            self,
            key: SupportsIndex
            | slice[SupportsIndex | None, SupportsIndex | None, SupportsIndex | None],
        ) -> str:
            if isinstance(key, slice):
                start, stop, step = key.indices(len(self))
                assert len(range(start, stop, step)) <= LogRedactor.MAX_RECORD
            return super().__getitem__(key)

    text = GuardedText("é " * (LogRedactor.MAX_RECORD * 10))
    shown = LogRedactor.settle(text)
    assert shown is not None
    assert "truncated" in shown
    assert len(shown.encode()) <= LogRedactor.MAX_RECORD


def test_span_and_events_rollback_together(tmp_path: Path) -> None:
    captured = InMemorySpanExporter()
    telemetry = open_telemetry(
        "test",
        SCHEMAS,
        TelemetryConfig(
            exporter="otlp",
            otlp_consent=True,
            otlp_endpoint="http://unused",
            exporter_factory=lambda _: (captured, CapturingMetricExporter()),
        ),
    )
    with telemetry.span("test.cli.run", 1):
        telemetry.event("test.cli.used", 1)
    telemetry.shutdown(5)
    store = SQLiteStore(tmp_path / "atomic.db", 90, {})
    conn = sqlite3.connect(store.path)
    conn.execute(
        "CREATE TRIGGER reject_events BEFORE INSERT ON span_events "
        "BEGIN SELECT RAISE(ABORT, 'failure'); END"
    )
    conn.commit()
    try:
        assert (
            SQLiteSpanExporter(store).export(captured.get_finished_spans())
            == SpanExportResult.FAILURE
        )
        assert conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM span_events").fetchone()[0] == 0
    finally:
        conn.close()
        store.close()


def test_existing_salt_is_read_before_any_creation(tmp_path: Path) -> None:
    root = tmp_path / ".punt-labs"
    root.mkdir(mode=0o700)
    (root / "telemetry.salt").write_bytes(b"s" * 32)
    with patch(
        "punt_observe.telemetry.salt.secrets.token_bytes", side_effect=AssertionError
    ):
        assert load_salt() == b"s" * 32


def test_salt_cache_avoids_repeated_reads_and_syncs() -> None:
    hash_identifier("first")
    with (
        patch("punt_observe.telemetry.salt.os.read", wraps=os.read) as read,
        patch("punt_observe.telemetry.salt.os.fsync", wraps=os.fsync) as sync,
    ):
        assert hash_identifier("second") == hash_identifier("second")
        assert read.call_count == 0
        assert sync.call_count == 0


def test_salt_link_syncs_parent_directory() -> None:
    synced: list[bool] = []
    original = os.fsync

    def sync(fd: int) -> None:
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        original(fd)

    with patch("punt_observe.telemetry.salt.os.fsync", side_effect=sync):
        load_salt()
    assert synced == [False, True]


def test_idle_metric_collections_write_no_rows(tmp_path: Path) -> None:
    telemetry = open_telemetry("test", SCHEMAS)
    assert telemetry._providers is not None
    conn = sqlite3.connect(tmp_path / ".punt-labs/test/telemetry.db")
    try:
        telemetry.count("test.cli.calls", 1, n=2)
        telemetry.record("test.cli.duration", 1, 1.5)
        telemetry._providers.reader.collect()
        assert conn.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0] == 2
        telemetry._providers.reader.collect()
        telemetry._providers.reader.collect()
        assert conn.execute("SELECT COUNT(*) FROM metric_points").fetchone()[0] == 2
        telemetry.count("test.cli.calls", 1, n=3)
        telemetry.record("test.cli.duration", 1, 2.5)
        telemetry._providers.reader.collect()
        assert conn.execute(
            "SELECT value FROM metric_points WHERE kind='counter' ORDER BY ts_ns"
        ).fetchall() == [(2.0,), (3.0,)]
    finally:
        telemetry.shutdown(5)
        conn.close()


def test_metric_interval_is_sixty_seconds() -> None:
    with patch(
        "punt_observe.telemetry.providers.PeriodicExportingMetricReader",
        wraps=PeriodicExportingMetricReader,
    ) as reader:
        telemetry = open_telemetry("test", SCHEMAS)
        try:
            assert reader.call_args.kwargs["export_interval_millis"] == 60_000
        finally:
            telemetry.shutdown(5)


@pytest.mark.parametrize("kind", [CountAttr, SizeAttr, LengthAttr])
def test_nonnegative_schema_types_reject_negative_min(
    kind: type[CountAttr | SizeAttr | LengthAttr],
) -> None:
    with pytest.raises(ValueError):
        SignalSchema("test.cli.run", 1, "span", {"n": kind(min=-1)})
