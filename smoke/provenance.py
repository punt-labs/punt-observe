"""Record fetched snapshot pins and the isolated installations' actual origin."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1]).resolve()
repos = Path(sys.argv[2]).resolve()
result = {}
for repo in ("punt-kit", "z-spec", "lux", "biff", "quarry", "vox"):
    project = tomllib.loads((root / repo / "pyproject.toml").read_text())
    lock = tomllib.loads((root / repo / "uv.lock").read_text())
    pin = project.get("tool", {}).get("uv", {}).get("sources", {}).get("punt-observe")
    locked = [p for p in lock["package"] if p["name"] == "punt-observe"]
    installed = subprocess.check_output(
        [
            str(root / repo / ".venv/bin/python"),
            "-c",
            (
                "import json,importlib.metadata as m,punt_observe.telemetry as t; "
                "d=m.distribution('punt-observe'); "
                "print(json.dumps(dict(version=d.version, module=t.__file__, "
                "direct_url=json.loads(d.read_text('direct_url.json') or '{}'))))"
            ),
        ],
        text=True,
    )
    result[repo] = {
        "main": subprocess.check_output(
            ["git", "-C", str(repos / repo), "rev-parse", "origin/main"], text=True
        ).strip(),
        "pyproject_dependency": [
            d
            for d in project["project"]["dependencies"]
            if d.startswith("punt-observe")
        ],
        "pyproject_source": pin,
        "lock": locked,
        "installed": json.loads(installed),
    }
(root / "tool-results" / "provenance.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
sys.stdout.write(json.dumps(result, indent=2) + "\n")
