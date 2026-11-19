"""Bound package-owned fork callbacks and reset only live telemetry instances."""

from __future__ import annotations

import gc
import multiprocessing
import os
import weakref
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, cast

import pytest

from punt_observe.telemetry import Telemetry, _fork, open_telemetry


def _check_child_resets(
    closed: list[Telemetry], live: Telemetry, output: Connection
) -> None:
    unchanged = all(
        cast(Any, instance)._lock is cast(Any, instance)._expected_lock
        and cast(Any, instance)._providers._pruner
        is cast(Any, instance)._expected_pruner
        for instance in closed
    )
    reset = (
        cast(Any, live)._lock is not cast(Any, live)._expected_lock
        and cast(Any, live)._providers.store._lock
        is not cast(Any, live)._expected_store_lock
    )
    live.shutdown()
    output.send((unchanged, reset))
    output.close()


def test_fork_hooks_are_constant_and_only_reset_live_instances(
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
    original = os.register_at_fork
    package_hooks: list[Callable[[], object]] = []
    sdk_hooks: list[Callable[[], object]] = []

    def register(**callbacks: Callable[[], object]) -> None:
        for callback in callbacks.values():
            if callback.__module__.startswith("punt_observe."):
                package_hooks.append(callback)
            elif callback.__module__.startswith("opentelemetry."):
                sdk_hooks.append(callback)
        original(**callbacks)

    monkeypatch.setattr(os, "register_at_fork", register)
    closed: list[Telemetry] = []
    own_after_first = 0
    sdk_after_first = 0
    for index in range(50):
        instance = open_telemetry("test", [])
        instance.shutdown()
        cast(Any, instance)._expected_lock = cast(Any, instance)._lock
        cast(Any, instance)._expected_pruner = cast(Any, instance)._providers._pruner
        closed.append(instance)
        if index == 0:
            own_after_first = len(package_hooks)
            sdk_after_first = len(sdk_hooks)
    collected = weakref.ref(closed.pop(0))
    gc.collect()
    assert collected() is None
    live = open_telemetry("test", [])
    cast(Any, live)._expected_lock = cast(Any, live)._lock
    cast(Any, live)._expected_store_lock = cast(Any, live)._providers.store._lock
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.get_context("fork").Process(
        target=_check_child_resets, args=(closed, live, child)
    )
    try:
        process.start()
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == 0
        assert parent.poll(1)
        child_resets = parent.recv()
        assert sdk_after_first > 0
        assert len(sdk_hooks) == sdk_after_first * 51
        assert (len(package_hooks), child_resets) == (own_after_first, (True, True))
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        live.shutdown()
        parent.close()
        child.close()


def test_fork_reset_failure_does_not_skip_other_live_instances(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Failing:
        def reset_after_fork(self) -> None:
            raise RuntimeError("SECRET_CONTENT")

    class Healthy:
        resets = 0

        def reset_after_fork(self) -> None:
            self.resets += 1

    failing, healthy = Failing(), Healthy()
    _fork.register(failing)
    _fork.register(healthy)
    try:
        cast(Any, _fork)._after_fork()
        assert healthy.resets == 1
        assert [r.getMessage() for r in caplog.records] == [
            "Telemetry dropped: fork_reset_failed"
        ]
        assert "SECRET_CONTENT" not in caplog.text
    finally:
        _fork.unregister(failing)
        _fork.unregister(healthy)
