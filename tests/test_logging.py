"""Verify private diagnostics, concurrent rotation and final-record redaction."""

from __future__ import annotations

import io

# pyright: reportPrivateUsage=false
import logging
import multiprocessing
import os
import re
import stat
import sys
import threading
from collections.abc import Iterator
from multiprocessing.synchronize import Barrier
from pathlib import Path
from unittest.mock import patch

import pytest

from punt_observe.logging import LoggingSetupError, configure_logging, escape
from punt_observe.logging.append_log import AtomicAppendLog
from punt_observe.logging.formatter import RedactingFormatter
from punt_observe.logging.handler import PrivateFileHandler
from punt_observe.logging.redactor import LogRedactor


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers = handlers
    root.setLevel(level)


def _writer(home: str, identity: int) -> None:
    os.environ["HOME"] = home
    configure_logging("test", "worker")
    for handler in logging.getLogger().handlers:
        if isinstance(handler, PrivateFileHandler):
            handler.sink._max_bytes = 4096
    for i in range(25):
        logging.getLogger("test.writer").info(
            "writer=%d line=%d %s", identity, i, "x" * 48
        )


def test_durable_sink_preserves_trailing_prose_punctuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = configure_logging("test", "hook")
    message = "Status (config=?, payload_ok=?)"
    with patch.object(RedactingFormatter, "formatTime", return_value="STAMP"):
        logging.getLogger("test.prose").info(message)
    assert path.read_text() == f"STAMP [INFO] test.prose: {message}\n"


def test_multiprocess_rotation(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_writer, args=(str(tmp_path), i)) for i in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    directory = tmp_path / ".punt-labs/test/logs"
    files = list(directory.glob("test.log*"))
    logs = [p for p in files if not p.name.endswith(".lock")]
    assert 2 <= len(logs) <= 6
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    seen: set[tuple[int, int]] = set()
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for path in logs:
        for line in path.read_text().splitlines():
            match = re.fullmatch(
                r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[INFO\] test.writer: "
                r"writer=(\d) line=(\d+) x{48}",
                line,
            )
            assert match, line
            key = (int(match[1]), int(match[2]))
            assert key not in seen
            seen.add(key)
    assert seen == {(i, j) for i in range(4) for j in range(25)}


def _capped_record(identity: int, index: int) -> str:
    prefix = f"writer={identity} line={index} "
    return prefix + "x" * (255 - len(prefix))


def _capped_writer(path: Path, identity: int, barrier: Barrier) -> None:
    sink = AtomicAppendLog(path, max_bytes=768)
    write = AtomicAppendLog._write_line

    def slow_write(self: AtomicAppendLog, line: bytes) -> bool:
        threading.Event().wait(0.01)
        return write(self, line)

    with patch.object(AtomicAppendLog, "_write_line", slow_write):
        for index in range(4):
            barrier.wait(timeout=15)
            sink.append(_capped_record(identity, index))


def test_concurrent_rotation_never_exceeds_cap(tmp_path: Path) -> None:
    path = tmp_path / "capped.log"
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(4)
    processes = [
        ctx.Process(target=_capped_writer, args=(path, identity, barrier))
        for identity in range(4)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
        files = list(tmp_path.glob("capped.log*"))
        records: list[str] = []
        for file in files:
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
            if file.name.endswith(".lock"):
                continue
            assert file.stat().st_size <= 768, (file.name, file.stat().st_size)
            data = file.read_bytes()
            assert data.endswith(b"\n")
            lines = data.splitlines(keepends=True)
            assert all(len(line) == 256 for line in lines)
            records.extend(line.decode().removesuffix("\n") for line in lines)
        assert len(records) == 16
        assert set(records) == {
            _capped_record(identity, index)
            for identity in range(4)
            for index in range(4)
        }
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)


@pytest.mark.parametrize(
    ("max_bytes", "records"),
    [
        (8, [""] * 8),
        (8, ["abc", "abc", "x"]),
        (8, ["0123456789", "x", "abcdefghij"]),
        (0, ["x" * 16] * 3),
        (-1, ["x" * 16] * 3),
    ],
)
def test_rotation_keeps_individual_records_whole(
    tmp_path: Path, max_bytes: int, records: list[str]
) -> None:
    path = tmp_path / "whole.log"
    sink = AtomicAppendLog(path, max_bytes=max_bytes)
    for record in records:
        sink.append(record)
    written: list[str] = []
    for file in tmp_path.glob("whole.log*"):
        if file.name.endswith(".lock"):
            continue
        data = file.read_bytes()
        lines = data.splitlines(keepends=True)
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert all(line.endswith(b"\n") for line in lines)
        if max_bytes > 0 and len(data) > max_bytes:
            assert len(lines) == 1
            assert len(lines[0]) > max_bytes
        written.extend(line.decode().removesuffix("\n") for line in lines)
    assert sorted(written) == sorted(records)


@pytest.mark.parametrize("backup_count", [0, 5])
def test_rotation_failure_returns_without_overfilling(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], backup_count: int
) -> None:
    path = tmp_path / "stalled.log"
    sink = AtomicAppendLog(path, max_bytes=4, backup_count=backup_count)
    sink.append("abc")
    with patch.object(Path, "replace", side_effect=PermissionError("denied")):
        sink.append("def")
    assert path.read_text() == "abc\n"
    assert "[append-log]" in capfd.readouterr().err


def test_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = configure_logging("test", "cli")
    configure_logging("test", "cli")
    logging.getLogger("test").info("hello")
    assert path.read_text().count("hello") == 1
    assert capsys.readouterr().out == ""
    assert logging.getLogger("httpx").level == logging.WARNING
    monkeypatch.setattr(sys, "stderr", None)
    configure_logging("test", "mcp")
    assert len(logging.getLogger().handlers) == 1
    with pytest.raises(ValueError):
        configure_logging("test", "unknown")
    with pytest.raises(ValueError):
        configure_logging("../bad", "cli")


def test_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / ".punt-labs/test/logs"
    directory.mkdir(parents=True)
    paths = [directory / f"test.log{s}" for s in ("", ".1", ".5", ".rotate.lock")]
    for path in paths:
        path.touch(mode=0o644)
    configure_logging("test", "hook")
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in paths)
    paths[0].unlink()
    paths[0].symlink_to(tmp_path / "victim")
    with pytest.raises(LoggingSetupError):
        configure_logging("test", "cli")
    paths[0].unlink()
    with (
        patch("os.fchmod", side_effect=PermissionError),
        pytest.raises(LoggingSetupError),
    ):
        configure_logging("test", "cli")


def test_final_redaction_and_cap(tmp_path: Path) -> None:
    path = tmp_path / "test.log"
    handler = PrivateFileHandler(path)
    handler.setFormatter(RedactingFormatter())
    record = logging.LogRecord(
        "test", logging.ERROR, __file__, 1, "%s", ("é " * 70000,), None
    )
    handler.handle(record)
    assert len(path.read_bytes()) <= 65536
    path.unlink()
    try:
        raise ValueError("https://u:SECRET@h/p?SECRET#SECRET")
    except ValueError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "oops\nforged\x00\x85\u2028",
            (),
            sys.exc_info(),
        )
        handler.handle(record)
    assert "SECRET" not in path.read_text()
    assert path.read_bytes().count(b"\n") == 1
    assert "\\u2028" in path.read_text()
    assert escape("\x00\x7f") == "\\x00\\x7f"
    with patch.object(LogRedactor, "settle", return_value=None):
        assert "record withheld" in RedactingFormatter().format(record)
    with patch.object(LogRedactor, "settle", side_effect=ValueError):
        assert "record withheld" in RedactingFormatter().format(record)
    assert LogRedactor.settle("") == ""
    assert len((LogRedactor.settle("é " * 70000) or "").encode()) <= 65536


def test_bearer_credentials_are_masked_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = configure_logging("test", "cli")
    logger = logging.getLogger("test")

    token = "fixture.header_signature-~+/=="
    logger.info("request Authorization: Bearer %s status=401", token)
    logger.info("headers:\nBearer %s\nend", token)
    logger.info("response:\x0bBearer %s: rejected", token)
    logger.info("headers %r", f"X:\tBearer\t{token}!")
    error = "received 'bEaReR\tfixture-exception'"
    try:
        raise ValueError(error)
    except ValueError:
        logger.exception("request failed")

    written = path.read_text(encoding="utf-8")
    assert "request Authorization: Bearer [bearer] status=401" in written
    assert r"headers:\nBearer [bearer]\nend" in written
    assert r"response:\x0bBearer [bearer]: rejected" in written
    assert r"headers 'X:\tBearer\t[bearer]!'" in written
    assert r"ValueError: received 'bEaReR\x09[bearer]'" in written
    assert token not in written
    assert "fixture-exception" not in written


def test_broken_stderr_cannot_echo_raw_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailOnce(io.StringIO):
        failed = False

        def write(self, text: str) -> int:
            if not self.failed:
                self.failed = True
                raise OSError("disk gone")
            return super().write(text)

    stream = FailOnce()
    notice = io.StringIO()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(sys, "__stderr__", notice)
    configure_logging("test", "cli")
    logging.getLogger("test").warning("fetch %s", "https://u:SECRET@h/p?q=SECRET")
    assert "SECRET" not in stream.getvalue() + notice.getvalue()
