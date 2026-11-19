"""Reproduce Qodo's remaining filesystem, complexity and schema findings."""

from __future__ import annotations

import operator
import time
from typing import Any, SupportsIndex, cast

import pytest

from punt_observe.logging import LogRedactor
from punt_observe.telemetry import (
    CountAttr,
    IntAttr,
    LengthAttr,
    SignalSchema,
    SizeAttr,
)


@pytest.mark.parametrize("kind", [IntAttr, CountAttr, SizeAttr, LengthAttr])
@pytest.mark.parametrize("bound", ["min", "max"])
@pytest.mark.parametrize("value", [float("nan"), 1.5, "3", True, False])
def test_integer_bounds_require_exact_integers(
    kind: type[IntAttr],
    bound: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        SignalSchema(
            "test.cli.run", 1, "span", {"n": cast(Any, kind)(**{bound: value})}
        )


@pytest.mark.parametrize("bounds", [{}, {"min": -2}, {"max": 2}, {"min": 0, "max": 2}])
def test_integer_bounds_keep_valid_open_and_closed_ranges(
    bounds: dict[str, int],
) -> None:
    SignalSchema(
        "test.cli.run",
        1,
        "span",
        {"n": IntAttr(min=bounds.get("min"), max=bounds.get("max"))},
    )


class CountBudgetText(str):
    scanned = 0

    def count(
        self,
        sub: str,
        start: SupportsIndex | None = None,
        end: SupportsIndex | None = None,
        /,
    ) -> int:
        lo = 0 if start is None else operator.index(start)
        hi = len(self) if end is None else operator.index(end)
        self.scanned += hi - lo
        assert self.scanned <= 4 * len(self), "quadratic prefix recount"
        return super().count(sub, lo, hi)


def test_peeling_trailing_brackets_has_a_linear_scan_budget() -> None:
    tail = "]" * 2000
    text = CountBudgetText("https://h/p" + tail)
    assert cast(Any, LogRedactor)._peel(text) == ("https://h/p", tail)


def test_many_near_limit_urls_keep_identical_output_quickly() -> None:
    tail = "]" * (LogRedactor.MAX_TOKEN - 64 - len("https://h/p"))
    text = " ".join(["https://h/p" + tail] * 16)
    expected = " ".join(["https://h/…(segments=1)" + tail] * 16)
    started = time.monotonic()
    assert LogRedactor.settle(text) == expected
    assert time.monotonic() - started < 2
