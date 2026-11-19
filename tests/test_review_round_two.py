"""Cover final handler redaction, safe health probes and export maintenance."""

from __future__ import annotations

import io
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanContext

from punt_observe.logging import AtomicAppendLog
from punt_observe.logging.handler import PrivateFileHandler, RedactingStreamHandler
from punt_observe.logging.redactor import LogRedactor
from punt_observe.telemetry import DurationAttr, EnumAttr, SchemaRegistry, SignalSchema
from punt_observe.telemetry.sqlite_exporter import SQLiteSpanExporter, SQLiteStore


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))


def _handler(kind: str, directory: Path) -> tuple[logging.Handler, Callable[[], str]]:
    if kind == "file":
        path = directory / "handler.log"
        return PrivateFileHandler(
            path
        ), lambda: path.read_text() if path.exists() else ""
    stream = io.StringIO()
    return RedactingStreamHandler(stream), stream.getvalue


def _record(message: str = "fetch %s") -> logging.LogRecord:
    args = ("https://u:SECRET@host/p?q=SECRET#SECRET",) if message else ()
    return logging.LogRecord(
        "review.handler", logging.WARNING, __file__, 1, message, args, None
    )


class InjectingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return "custom https://u:SECRET@host/p?q=SECRET#SECRET"


class BrokenFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        raise ValueError("SECRET formatter failure")


@pytest.mark.parametrize("kind", ["file", "stream"])
@pytest.mark.parametrize("formatting", ["default", "plain", "injected"])
def test_handlers_redact_without_trusting_the_formatter(
    tmp_path: Path,
    kind: str,
    formatting: str,
) -> None:
    handler, read = _handler(kind, tmp_path)
    if formatting == "plain":
        handler.setFormatter(logging.Formatter("custom %(message)s"))
    elif formatting == "injected":
        handler.setFormatter(InjectingFormatter())
    try:
        handler.handle(_record())
        output = read()
        assert "SECRET" not in output
        assert "https://host/" in output
        if formatting != "default":
            assert output.startswith("custom ")
    finally:
        handler.close()


@pytest.mark.parametrize("kind", ["file", "stream"])
@pytest.mark.parametrize("failure", ["unsettled", "redaction_error", "format_error"])
def test_handlers_withhold_on_final_redaction_or_format_failure(
    tmp_path: Path,
    kind: str,
    failure: str,
) -> None:
    handler, read = _handler(kind, tmp_path)
    handler.setFormatter(
        BrokenFormatter() if failure == "format_error" else InjectingFormatter()
    )
    kwargs: dict[str, Any] = (
        {"return_value": None}
        if failure == "unsettled"
        else {"side_effect": RuntimeError("SECRET redaction failure")}
        if failure == "redaction_error"
        else {}
    )
    try:
        if failure == "format_error":
            handler.handle(_record())
        else:
            with patch.object(LogRedactor, "settle", **kwargs):
                handler.handle(_record())
        output = read()
        assert "record withheld" in output
        assert "WARNING" in output
        assert "review.handler" in output
        assert "SECRET" not in output
    finally:
        handler.close()


@pytest.mark.parametrize("kind", ["file", "stream"])
def test_handler_default_accepts_empty_records(tmp_path: Path, kind: str) -> None:
    handler, read = _handler(kind, tmp_path)
    try:
        handler.setFormatter(None)
        handler.handle(_record(""))
        assert read() == "\n"
    finally:
        handler.close()


@pytest.mark.parametrize("level", [0, 1, 2])
@pytest.mark.parametrize("existing", [False, True])
def test_health_refuses_symlinked_directory_levels(
    tmp_path: Path,
    level: int,
    existing: bool,
) -> None:
    root = tmp_path / ".punt-labs"
    directory = root / "test/logs"
    directory.mkdir(parents=True)
    path = directory / "test.log"
    if existing:
        path.write_text("before\n")
        path.with_name(path.name + ".rotate.lock").touch()
    link = [root, root / "test", directory][level]
    real = link.with_name(link.name + "-real")
    link.rename(real)
    link.symlink_to(real, target_is_directory=True)
    sink = AtomicAppendLog(path)
    sink.append("not written")
    assert path.read_text() == "before\n" if existing else not path.exists()
    assert sink.health()["status"] == "degraded"


def test_healthy_probe_does_not_create_missing_directories(tmp_path: Path) -> None:
    path = tmp_path / ".punt-labs/test/logs/test.log"
    assert AtomicAppendLog(path).health()["status"] == "healthy"
    assert not (tmp_path / ".punt-labs").exists()


@pytest.mark.parametrize(
    "values", ["ok", (), frozenset[str](), ["ok"], {"ok"}, ("ok", 1), (None,), None]
)
def test_enum_definition_rejects_invalid_containers_and_members(values: object) -> None:
    with pytest.raises(ValueError):
        SignalSchema(
            "test.cli.run", 1, "span", {"outcome": cast(Any, EnumAttr)(values)}
        )


@pytest.mark.parametrize("values", [("ok", "error"), frozenset({"ok", "error"})])
def test_enum_accepts_only_whole_declared_strings(
    values: tuple[str, ...] | frozenset[str],
) -> None:
    registry = SchemaRegistry(
        [SignalSchema("test.cli.run", 1, "span", {"outcome": EnumAttr(values)})]
    )
    assert registry.validate("test.cli.run", 1, {"outcome": "ok"}) == {"outcome": "ok"}
    assert isinstance(registry.validate("test.cli.run", 1, {"outcome": "o"}), str)
    assert isinstance(registry.validate("test.cli.run", 1, {"outcome": "k"}), str)


@pytest.mark.parametrize("unit", ["s", "By", "1", "", None, 1, True])
def test_duration_definition_rejects_non_millisecond_units(unit: object) -> None:
    with pytest.raises(ValueError):
        SignalSchema(
            "test.cli.run", 1, "span", {"duration": cast(Any, DurationAttr)(unit)}
        )


def test_duration_milliseconds_accept_zero_and_fractional_values() -> None:
    registry = SchemaRegistry(
        [SignalSchema("test.cli.run", 1, "span", {"duration": DurationAttr()})]
    )
    for value in (0, 0.5):
        assert registry.validate("test.cli.run", 1, {"duration": value}) == {
            "duration": value
        }


def test_committed_writes_succeed_when_pruning_fails(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db", 90, {})
    conn = sqlite3.connect(store.path)
    try:
        with (
            patch.object(
                store,
                "prune",
                side_effect=sqlite3.OperationalError("SECRET prune failure"),
            ),
            patch.object(store, "_last_prune", time.monotonic() - 3601),
            patch("punt_observe.telemetry._diagnostics._last", float("-inf")),
            patch("punt_observe.telemetry._diagnostics.logger.warning") as warning,
        ):
            result = store.write("INSERT INTO meta VALUES (?,?)", [("first", "1")])
            assert conn.execute(
                "SELECT value FROM meta WHERE key='first'"
            ).fetchone() == ("1",)
            assert result
            assert store.write("INSERT INTO meta VALUES (?,?)", [("second", "2")])
            warning.assert_called_once_with(
                "Telemetry dropped: %s", "sqlite_prune_failed"
            )
    finally:
        conn.close()
        store.close()


def test_exporter_does_not_request_retry_after_commit_and_prune_failure(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "spans.db", 90, {})
    conn = sqlite3.connect(store.path)
    span = ReadableSpan(
        name="test.cli.run",
        context=SpanContext(1, 2, False),
        start_time=time.time_ns(),
        end_time=time.time_ns(),
    )
    try:
        with (
            patch.object(store, "prune", side_effect=OSError),
            patch.object(store, "_last_prune", time.monotonic() - 3601),
        ):
            result = SpanExportResult.FAILURE
            for _ in range(2):
                result = SQLiteSpanExporter(store).export([span])
                if result == SpanExportResult.SUCCESS:
                    break
        assert conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 1
        assert result == SpanExportResult.SUCCESS
    finally:
        conn.close()
        store.close()
