"""Run the installed wheel smoke flight; save evidence outside the checkout."""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from inspect_store import inspect

SCRIPTS = Path(__file__).resolve().parent
OUTPUT = Path(sys.argv[1]).resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)


def environment(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "SMOKE_OUTPUT": str(OUTPUT),
    }


def run(mode: str, home: Path, extra: dict[str, str] | None = None) -> dict:
    env = environment(home) | (extra or {})
    script = "otlp.py" if mode == "otlp" else "cli.py"
    start = time.perf_counter()
    try:
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / script), mode],
            env=env,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {
            "exit": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
            "elapsed_s": time.perf_counter() - start,
        }
    except subprocess.TimeoutExpired:
        return {"exit": "timeout"}


def fresh(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=label + "-", dir=OUTPUT))


report: dict[str, object] = {}
for mode in ("emit", "invalid", "fork", "idle"):
    home = fresh(mode)
    execution = run(mode, home)
    store = inspect(home, "smoketool")
    counts = store.get("counts", {})
    expected = {"meta": 1, "spans": 1, "span_events": 1, "metric_points": 2}
    if mode == "invalid":
        expected.update(spans=0, span_events=0, metric_points=0)
    elif mode == "fork":
        expected.update(spans=4, span_events=4, metric_points=8)
    elif mode == "idle":
        expected.update(spans=0, span_events=0, metric_points=0)
    report[mode] = {
        "pass": execution["exit"] == 0
        and counts == expected
        and store.get("integrity") == "ok"
        and not store.get("secret_hits"),
        "home": str(home),
        "execution": execution,
        "store": store,
    }

report["off"] = []
for mode, extra in (
    ("emit", {"PUNT_TELEMETRY": "off"}),
    ("emit", {"SMOKETOOL_TELEMETRY": "off"}),
    ("disabled", {}),
):
    home = fresh("off")
    execution = run(mode, home, extra)
    data = json.loads(execution.get("stdout", "{}"))
    report["off"].append(
        {
            "pass": execution["exit"] == 0
            and not data.get("db_exists", True)
            and data.get("extra_threads_before_shutdown") == [],
            "switch": extra or "enabled=False",
            "execution": execution,
        }
    )

rounds = []
for index in range(5):
    home = fresh("concurrent")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        executions = list(pool.map(lambda _, home=home: run("emit", home), range(8)))
    store = inspect(home, "smoketool")
    rounds.append(
        {
            "round": index + 1,
            "pass": all(e["exit"] == 0 for e in executions)
            and store.get("counts")
            == {"meta": 1, "spans": 8, "span_events": 8, "metric_points": 16}
            and store.get("integrity") == "ok",
            "executions": executions,
            "store": store,
        }
    )
report["concurrency"] = rounds
home = fresh("otlp")
execution = run("otlp", home)
data = json.loads(execution.get("stdout", "{}"))
requests = data.get("requests", [])
report["otlp"] = {
    "pass": execution["exit"] == 0
    and not data.get("db_exists", True)
    and {r["path"] for r in requests} == {"/v1/traces", "/v1/metrics"}
    and all(
        "x-smoke-secret" not in {k.lower() for k in r["headers"]} for r in requests
    ),
    "execution": execution,
    "data": data,
}
timing = subprocess.run(
    [
        sys.executable,
        "-c",
        (
            "import time,json; start=time.perf_counter(); "
            "import punt_observe.telemetry as t; "
            "import_ms=(time.perf_counter()-start)*1000; start=time.perf_counter(); "
            "x=t.open_telemetry('smoketool',[]); x.shutdown(timeout_s=2); "
            "print(json.dumps(dict(import_ms=import_ms,open_shutdown_ms="
            "(time.perf_counter()-start)*1000)))"
        ),
    ],
    env=environment(fresh("timing")),
    capture_output=True,
    text=True,
    timeout=20,
)
report["timing"] = json.loads(timing.stdout)
(OUTPUT / "package.json").write_text(json.dumps(report, indent=2) + "\n")
sys.stdout.write(
    json.dumps(
        {
            k: ([r["pass"] for r in v] if isinstance(v, list) else v.get("pass", v))
            for k, v in report.items()
        },
        indent=2,
    )
    + "\n"
)
sys.exit(
    int(
        not all(
            all(r["pass"] for r in value)
            if isinstance(value, list)
            else value.get("pass", True)
            for value in report.values()
        )
    )
)
