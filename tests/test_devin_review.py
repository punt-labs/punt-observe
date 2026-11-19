"""Reproduce privacy and lifecycle findings through public entry points."""

from __future__ import annotations

import multiprocessing
import os
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from punt_observe._private_fs import PrivatePaths
from punt_observe.logging.redactor import LogRedactor


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
