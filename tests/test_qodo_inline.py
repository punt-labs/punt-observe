"""Reproduce Qodo's remaining filesystem, complexity and schema findings."""

from __future__ import annotations

from typing import Any, cast

import pytest

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
