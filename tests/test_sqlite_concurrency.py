"""Keep SQLite's shared-memory locks intact under concurrent process startup."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import stat
from multiprocessing.synchronize import Barrier
from pathlib import Path

import pytest

from punt_observe._private_fs import PrivatePaths
from punt_observe.telemetry import SignalSchema, open_telemetry
from punt_observe.telemetry.sqlite_exporter import SQLiteStore


def _write_events(home: Path, identity: int, barrier: Barrier) -> None:
    os.environ["HOME"] = str(home)
    schema = SignalSchema("test.concurrent.event", 1, "event", {})
    path = home / ".punt-labs/test/telemetry.db"
    barrier.wait(timeout=30)
    for _ in range(12):
        telemetry = open_telemetry("test", [schema])
        try:
            for _ in range(500 + identity * 100):
                telemetry.event(schema.name, 1)
            for suffix in ("", "-wal", "-shm", "-journal"):
                file = path.with_name(path.name + suffix)
                try:
                    mode = file.stat().st_mode
                except FileNotFoundError:
                    continue
                assert stat.S_IMODE(mode) == 0o600
        finally:
            telemetry.shutdown(10)


def test_concurrent_telemetry_startup_preserves_sqlite_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "PUNT_TELEMETRY",
        "TEST_TELEMETRY",
        "PUNT_TELEMETRY_EXPORTER",
        "PUNT_TELEMETRY_OTLP_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(6)
    processes = [
        ctx.Process(target=_write_events, args=(tmp_path, identity, barrier))
        for identity in range(6)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(60)
        assert [p.exitcode for p in processes] == [0] * 6
        path = tmp_path / ".punt-labs/test/telemetry.db"
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("SELECT count(*) FROM spans").fetchone()[0] == (
                12 * sum(500 + identity * 100 for identity in range(6))
            )
            for suffix in ("", "-wal", "-shm", "-journal"):
                file = path.with_name(path.name + suffix)
                if file.exists():
                    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)


def test_store_does_not_secure_live_sqlite_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connected = False
    connect = sqlite3.connect
    secure = PrivatePaths.secure

    def checked_connect(
        database: Path, *, timeout: float, check_same_thread: bool
    ) -> sqlite3.Connection:
        nonlocal connected
        connection = connect(
            database, timeout=timeout, check_same_thread=check_same_thread
        )
        connected = True
        return connection

    def checked_secure(paths: PrivatePaths) -> None:
        assert not connected, "opening a live SQLite file releases its POSIX locks"
        secure(paths)

    monkeypatch.setattr(sqlite3, "connect", checked_connect)
    monkeypatch.setattr(PrivatePaths, "secure", checked_secure)
    store = SQLiteStore(tmp_path / "telemetry.db", 1, {})
    try:
        assert store.write("INSERT INTO spans (end_ns) VALUES (?)", [(0,)])
        store.prune()
    finally:
        store.close()
