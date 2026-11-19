"""Reproduce privacy and lifecycle findings through public entry points."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sqlite3
from multiprocessing.connection import Connection
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from punt_observe._private_fs import PrivatePaths
from punt_observe.logging.redactor import LogRedactor
from punt_observe.telemetry import TelemetryConfig, open_telemetry
from punt_observe.telemetry.sqlite_exporter import SQLiteStore
from tests.test_telemetry import SCHEMAS, CapturingMetricExporter


@pytest.mark.parametrize(
    "text", ["https://h/o'brien/SECRET", "'https://h/o'brien/SECRET'"]
)
def test_apostrophe_does_not_end_url_path_redaction(text: str) -> None:
    shown = LogRedactor.settle(text)
    assert shown is not None
    assert "SECRET" not in shown
    assert "brien" not in shown
    assert "segments=2" in shown


@pytest.mark.parametrize("path", ["/api/x", "api/x"])
def test_request_query_cannot_be_consumed_as_userinfo(path: str) -> None:
    shown = LogRedactor.settle(path + "?token=abc:def@host/SECRET")
    assert shown == path + "?[query]"


def _open_fifo(path: Path, connection: Connection) -> None:
    try:
        fd = PrivatePaths(path).open_private(os.O_RDONLY)
    except OSError:
        connection.send("rejected")
    else:
        os.close(fd)
        connection.send("opened")
    finally:
        connection.close()


def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "fifo"
    os.mkfifo(path, 0o600)
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.get_context("fork").Process(
        target=_open_fifo, args=(path, child)
    )
    process.start()
    process.join(1)
    blocked = process.is_alive()
    if blocked:
        process.terminate()
        process.join(5)
    try:
        assert not blocked, "open_private blocked opening a FIFO"
        assert parent.poll(1)
        assert parent.recv() == "rejected"
    finally:
        parent.close()
        child.close()


def test_incremental_vacuum_reclaims_entire_freelist(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "vacuum.db", 90, {})
    conn = sqlite3.connect(store.path)
    try:
        conn.execute("CREATE TABLE scratch (data BLOB)")
        conn.execute("INSERT INTO scratch VALUES (zeroblob(2000000))")
        conn.execute("DROP TABLE scratch")
        conn.commit()
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 100
        store.prune()
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    finally:
        conn.close()
        store.close()


def test_deferred_task_event_outlives_its_inherited_span() -> None:
    spans = InMemorySpanExporter()
    telemetry = open_telemetry(
        "test",
        SCHEMAS,
        TelemetryConfig(
            exporter="otlp",
            otlp_consent=True,
            otlp_endpoint="http://unused",
            exporter_factory=lambda _: (spans, CapturingMetricExporter()),
        ),
    )

    async def scenario() -> None:
        ready = asyncio.Event()

        async def deferred() -> None:
            await ready.wait()
            telemetry.event("test.cli.used", 1)

        with telemetry.span("test.cli.run", 1):
            task = asyncio.create_task(deferred())
        ready.set()
        await task

    try:
        asyncio.run(scenario())
    finally:
        telemetry.shutdown(5)
    exported = spans.get_finished_spans()
    assert [span.name for span in exported] == ["test.cli.run", "test.cli.used"]
    assert exported[1].parent is None
    assert exported[1].start_time == exported[1].end_time
