"""Reproduce Qodo's remaining filesystem, complexity and schema findings."""

from __future__ import annotations

import operator
import os
import stat
import time
from pathlib import Path
from typing import Any, SupportsIndex, cast
from unittest.mock import patch

import pytest

from punt_observe._private_fs import PrivatePaths
from punt_observe.logging import LogRedactor
from punt_observe.logging.append_log import AtomicAppendLog
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


@pytest.mark.parametrize("existing", [False, True])
def test_custom_directory_rejects_a_symlinked_ancestor_before_chmod(
    tmp_path: Path,
    existing: bool,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    child = target / "logs"
    if existing:
        child.mkdir(mode=0o755)
        child.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        PrivatePaths.ensure_directory(link / "logs")
    if existing:
        assert stat.S_IMODE(child.stat().st_mode) == 0o755
    else:
        assert not child.exists()


def test_private_open_rejects_a_symlinked_custom_parent(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "logs").mkdir()
    leaf = target / "logs" / "out.log"
    leaf.write_text("unchanged")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        fd = PrivatePaths(link / "logs" / "out.log").open_private(os.O_WRONLY)
        os.close(fd)
    assert leaf.read_text() == "unchanged"


def test_append_cannot_redirect_through_a_custom_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "logs").mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    AtomicAppendLog(link / "logs" / "out.log").append("must not be written")
    assert list((target / "logs").iterdir()) == []


def test_custom_paths_allow_a_resolved_trusted_temp_root(tmp_path: Path) -> None:
    target = tmp_path / "trusted-temp"
    target.mkdir()
    alias = tmp_path / "system-temp-alias"
    alias.symlink_to(target, target_is_directory=True)
    with patch("punt_observe._private_fs.tempfile.gettempdir", return_value=str(alias)):
        path = alias / "new" / "logs" / "out.log"
        AtomicAppendLog(path).append("safe")
    assert (target / "new" / "logs" / "out.log").read_text() == "safe\n"
