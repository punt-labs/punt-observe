"""Reproduce privacy and lifecycle findings through public entry points."""

from __future__ import annotations

import pytest

from punt_observe.logging.redactor import LogRedactor


@pytest.mark.parametrize("text", ["https://h/o'brien/SECRET", "'https://h/o'brien/SECRET'"])
def test_apostrophe_does_not_end_url_path_redaction(text: str) -> None:
    shown = LogRedactor.settle(text)
    assert shown is not None
    assert "SECRET" not in shown
    assert "brien" not in shown
    assert "segments=2" in shown
