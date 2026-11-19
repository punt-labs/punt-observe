"""Install an explicit release wheel or one produced by a fresh build."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from gates import built_wheel, require


def install(venv: str, wheel: Path | None = None) -> None:
    if wheel is not None:
        require(
            wheel.is_file() and wheel.suffix == ".whl",
            "install: expected an existing wheel file",
        )
        subprocess.run(
            ["uv", "pip", "install", "--reinstall", "--python", venv, str(wheel)],
            check=True,
        )
        return
    with tempfile.TemporaryDirectory(prefix="punt-smoke-build-") as directory:
        destination = Path(directory)
        subprocess.run(["uv", "build", "--out-dir", str(destination)], check=True)
        install(venv, built_wheel(destination))


if __name__ == "__main__":
    install(
        sys.argv[1], Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
    )
