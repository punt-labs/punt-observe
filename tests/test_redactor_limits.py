"""Retain quarry's cap, bounded-cost and non-settling reference cases."""

from __future__ import annotations

import logging
import time

import pytest

from punt_observe.logging.redactor import LogRedactor
from tests.redaction import redacted


def _seconds(text: str) -> float:
    started = time.perf_counter()
    redacted(text)
    return time.perf_counter() - started


def test_a_record_over_the_cap_is_truncated_with_a_visible_marker() -> None:
    cap = LogRedactor.MAX_RECORD
    shown = redacted("a " * cap)
    assert shown.endswith(f"[…truncated {cap} bytes]")
    assert len(shown.encode()) <= cap


def test_a_record_at_the_cap_is_kept_whole() -> None:
    text = "w" * 10 + " x" * ((LogRedactor.MAX_RECORD - 10) // 2)
    assert redacted(text) == text


def test_a_secret_beyond_the_cap_is_never_kept() -> None:
    text = "x " * LogRedactor.MAX_RECORD + "https://u:SECRET@h/p"
    assert "SECRET" not in redacted(text)


def test_cost_is_bounded_by_the_cap_not_the_record() -> None:
    small = min(_seconds("w " * (LogRedactor.MAX_RECORD // 2)) for _ in range(3))
    huge = min(_seconds("w " * (8 * LogRedactor.MAX_RECORD)) for _ in range(3))
    assert huge < 4 * max(small, 1e-4)


def test_text_with_no_url_characters_costs_less_than_a_url_laden_line() -> None:
    plain = min(_seconds("word " * 12_000) for _ in range(3))
    urls = min(_seconds("https://u:p@h.test/a/b?q=1 " * 2_200) for _ in range(3))
    assert plain < urls


def test_a_single_run_over_the_record_cap_still_shows_the_marker() -> None:
    shown = redacted("x" * (LogRedactor.MAX_RECORD + 4_000))
    assert shown.endswith(" […truncated 4000 bytes]")
    assert shown.startswith("[…]")


def test_the_marker_is_separate_from_the_kept_text() -> None:
    cap = LogRedactor.MAX_RECORD
    shown = redacted("a " * cap)
    assert shown.endswith(f" […truncated {cap} bytes]")
    assert "bytes][…" not in shown


def test_a_marker_survives_a_second_redaction_unchanged() -> None:
    once = redacted("x" * (LogRedactor.MAX_RECORD + 10))
    assert redacted(once) == once


def test_a_record_that_never_settles_says_none_and_nothing_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    def never_settles(text: str) -> str:
        calls.append(text)
        return text + "x"

    monkeypatch.setattr(LogRedactor, "_once", never_settles)
    with caplog.at_level(logging.DEBUG):
        shown = LogRedactor.settle("https://u:SECRET@h/p")
    assert shown is None
    assert len(calls) == LogRedactor.PASSES
    assert caplog.records == []


def test_a_truncated_record_that_never_settles_says_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never_settles(text: str) -> str:
        return text + "x"

    monkeypatch.setattr(LogRedactor, "_once", never_settles)
    assert LogRedactor.settle("a" * (LogRedactor.MAX_RECORD + 5)) is None


def test_a_truncated_record_that_settles_keeps_its_marker() -> None:
    assert redacted("a" * (LogRedactor.MAX_RECORD + 5)).endswith(
        " […truncated 5 bytes]"
    )
