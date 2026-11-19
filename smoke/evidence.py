"""Persist smoke evidence even when its parent has not been created yet."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def save(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n")
