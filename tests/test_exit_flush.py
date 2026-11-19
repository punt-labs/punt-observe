"""Exercise installed-wheel exit persistence, without periodic-export sleeps."""

from __future__ import annotations

import atexit
import os
import signal
import sqlite3
import subprocess
import sys
import sysconfig
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from punt_observe.telemetry import (
    SignalSchema,
    TelemetryConfig,
    _diagnostics,
    _exit,
    open_telemetry,
)
from punt_observe.telemetry.providers import Providers

exit_registry = cast(Any, _exit)

SCRIPT = r"""
import sys
sys.path.append(sys.argv[2])  # Reuse only third-party dependencies of the test runner.
import contextlib, os, signal, time
from pathlib import Path
import punt_observe.telemetry as module
from punt_observe.telemetry import SignalSchema, open_telemetry
assert str(Path(module.__file__).resolve()).startswith(sys.prefix)
mode = sys.argv[1]
schemas = [SignalSchema("exit.cli.count", 1, "counter"),
           SignalSchema("exit.cli.duration", 1, "histogram", unit="ms"),
           SignalSchema("exit.cli.run", 1, "span"),
           SignalSchema("exit.cli.event", 1, "event")]

def emit(t):
    with t.span("exit.cli.run", 1):
        t.event("exit.cli.event", 1)
    t.count("exit.cli.count", 1, n=3)
    t.record("exit.cli.duration", 1, 12.5)

def ready():
    (Path.home() / "ready").touch()

@contextlib.contextmanager
def usage():
    t = open_telemetry("exit", schemas)
    try:
        yield t
    finally:
        t.shutdown(timeout_s=2)

def main():
    if mode == "lux":
        # Lux owns usage outside uvicorn.Server.run/capture_signals. Uvicorn
        # restores and re-raises the captured SIGTERM before that scope unwinds.
        with usage() as t:
            captured = []
            original = signal.signal(
                signal.SIGTERM, lambda sig, frame: captured.append(sig)
            )
            try:
                emit(t)
                ready()
                while not captured:
                    time.sleep(0.01)
            finally:
                signal.signal(signal.SIGTERM, original)
                for sig in reversed(captured):
                    signal.raise_signal(sig)
        return
    t = open_telemetry("exit", schemas)
    if mode in ("system_exit", "keyboard_interrupt"):
        def handler(sig, frame):
            if mode == "system_exit":
                raise SystemExit(0)
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, handler)
    emit(t)
    if mode == "explicit":
        t.shutdown(timeout_s=2)
    elif mode in ("system_exit", "keyboard_interrupt", "default"):
        ready()
        while True:
            signal.pause()
    elif mode == "sys_exit":
        sys.exit(0)
    elif mode == "fork_idle":
        pid = os.fork()
        if pid == 0:
            sys.exit(0)
        assert os.waitpid(pid, 0)[1] == 0
        import sqlite3
        with sqlite3.connect(Path.home() / ".punt-labs/exit/telemetry.db") as conn:
            assert conn.execute("SELECT count(*) FROM metric_points").fetchone() == (0,)

main()
"""


@pytest.fixture(scope="module")
def installed_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("installed-exit")
    project = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(root)],
        cwd=project,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(root / "venv")],
        check=True,
        capture_output=True,
    )
    python = root / "venv/bin/python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            str(next(root.glob("*.whl"))),
        ],
        check=True,
        capture_output=True,
    )
    return python


@pytest.fixture
def executions() -> Iterator[list[subprocess.Popen[str]]]:
    processes: list[subprocess.Popen[str]] = []
    yield processes
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize(
    "mode",
    [
        "explicit",
        "return",
        "sys_exit",
        "system_exit",
        "keyboard_interrupt",
        "default",
        "lux",
        "fork_idle",
    ],
)
def test_installed_exit_persistence(
    installed_python: Path,
    tmp_path: Path,
    executions: list[subprocess.Popen[str]],
    mode: str,
) -> None:
    process = subprocess.Popen(
        [str(installed_python), "-c", SCRIPT, mode, sysconfig.get_path("purelib")],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    executions.append(process)
    if mode in ("system_exit", "keyboard_interrupt", "default", "lux"):
        deadline = time.monotonic() + 10
        while not (tmp_path / "ready").exists() and process.poll() is None:
            assert time.monotonic() < deadline, "subprocess did not emit"
            time.sleep(0.01)
        assert (tmp_path / "ready").exists(), process.communicate(timeout=5)
        process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode in (0, -signal.SIGINT, -signal.SIGTERM), (stdout, stderr)
    with sqlite3.connect(tmp_path / ".punt-labs/exit/telemetry.db") as conn:
        counts = tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("spans", "span_events", "metric_points")
        )
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        if mode in ("default", "lux"):
            assert process.returncode == -signal.SIGTERM
            assert counts == (0, 0, 0), "default-disposition SIGTERM bypasses cleanup"
        else:
            assert counts == (1, 1, 2), (mode, counts, stderr)
            assert conn.execute(
                "SELECT value FROM metric_points WHERE kind='counter'"
            ).fetchone() == (3.0,)
            assert conn.execute(
                "SELECT count, sum FROM metric_points WHERE kind='histogram'"
            ).fetchone() == (1, 12.5)


def test_exit_budget_is_shared_and_failures_do_not_skip_healthy_work(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(_exit, "_TIMEOUT_S", 0.05)
    _diagnostics.reset_after_fork()
    release = threading.Event()
    healthy = threading.Event()

    def fail(timeout: float) -> None:
        raise SystemExit("SECRET_REJECTED_DATA")

    def block(timeout: float) -> None:
        release.wait(5)

    blocked = [_exit.ExitFlush(block) for _ in range(3)]
    failed = _exit.ExitFlush(fail)
    completed = _exit.ExitFlush(lambda timeout: healthy.set())
    try:
        started = time.monotonic()
        exit_registry._shutdown_at_exit()
        assert time.monotonic() - started < 0.5
        assert healthy.is_set()
        assert "SECRET_REJECTED_DATA" not in caplog.text
        assert "Telemetry dropped: exit_flush_failed" in caplog.text
    finally:
        release.set()
        for task in [*blocked, failed, completed]:
            task.wait(time.monotonic() + 1)
            task.cancel()


def test_exit_registration_is_constant_and_explicit_shutdown_cancels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    registrations: list[object] = []

    def register(callback: object) -> None:
        registrations.append(callback)

    monkeypatch.setattr(atexit, "register", register)
    pending_before = set(exit_registry._pending)
    handlers = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    for _ in range(50):
        telemetry = open_telemetry(
            "exit", [SignalSchema("exit.cli.count", 1, "counter")]
        )
        telemetry.count("exit.cli.count", 1)
        telemetry.shutdown()
    assert registrations == []
    assert exit_registry._pending == pending_before
    assert handlers == (
        signal.getsignal(signal.SIGTERM),
        signal.getsignal(signal.SIGINT),
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_no_exit_work_without_emission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    before = set(exit_registry._pending)
    telemetry = open_telemetry("exit", config=TelemetryConfig(enabled=enabled))
    try:
        assert exit_registry._pending == before
        exit_registry._shutdown_at_exit()
        assert not (tmp_path / ".punt-labs/exit/telemetry.db").exists() or enabled
    finally:
        telemetry.shutdown()


@pytest.mark.parametrize("fail", [False, True])
def test_shutdown_flushes_both_providers_before_closing_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    telemetry = open_telemetry("exit", [SignalSchema("exit.cli.count", 1, "counter")])
    telemetry.count("exit.cli.count", 1)
    bundle = cast(Providers | None, cast(Any, telemetry)._providers)
    assert bundle is not None and bundle.store is not None
    order: list[str] = []
    budgets: list[float] = []
    metric_flush = bundle.reader.force_flush
    span_flush = bundle.traces.force_flush
    close = bundle.store.close

    def flush_metrics(timeout_millis: float) -> bool:
        order.append("metrics")
        budgets.append(timeout_millis)
        if fail:
            raise RuntimeError("SECRET_REJECTED_DATA")
        return metric_flush(timeout_millis=timeout_millis)

    def flush_spans(timeout_millis: int = 30000) -> bool:
        order.append("spans")
        budgets.append(timeout_millis)
        return span_flush(timeout_millis=timeout_millis)

    def close_store() -> None:
        order.append("close")
        close()

    _diagnostics.reset_after_fork()
    monkeypatch.setattr(bundle.reader, "force_flush", flush_metrics)
    monkeypatch.setattr(bundle.traces, "force_flush", flush_spans)
    monkeypatch.setattr(bundle.store, "close", close_store)
    telemetry.shutdown(timeout_s=0.5)
    assert order == ["metrics", "spans", "close"]
    assert 0 < budgets[1] <= budgets[0] <= 500
    assert "SECRET_REJECTED_DATA" not in caplog.text
    if fail:
        assert "Telemetry dropped: flush_failed" in caplog.text
