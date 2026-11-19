"""Install only the artifact produced by this build, never a stale dist glob."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from gates import built_wheel


def install(venv: str) -> None:
    with tempfile.TemporaryDirectory(prefix="punt-smoke-build-") as directory:
        destination = Path(directory)
        subprocess.run(["uv", "build", "--out-dir", str(destination)], check=True)
        wheel = built_wheel(destination)
        subprocess.run(
            ["uv", "pip", "install", "--reinstall", "--python", venv, str(wheel)],
            check=True,
        )


if __name__ == "__main__":
    install(sys.argv[1])
