"""Smoke real local daemon-backed CLI operations, without models or GUI fakes."""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

from http_mcp import call as http_mcp
from inspect_store import inspect
from run_tools import (
    OUTPUT,
    ROOT,
    SECRET,
    environment,
    fresh,
    grep_private,
    invoke,
    stdio,
)


def flight(
    repo: str, switch: str | None = None, *, wait_for_export: bool = False
) -> dict:
    home = fresh(repo + "-daemon")
    env = environment(home, repo) | ({switch: "off"} if switch else {})
    binary = ROOT / repo / ".venv/bin"
    command = (
        [str(binary / "luxd-hub"), "--port", "0"]
        if repo == "lux"
        else [str(binary / "quarryd"), "--port", "0"]
    )
    result = {"home": str(home), "command": command, "switch": switch}
    with (home / "daemon-output.txt").open("w") as log:
        daemon = subprocess.Popen(command, env=env, cwd=home, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 35
            port = None
            while daemon.poll() is None and time.monotonic() < deadline:
                files = list((home / ".punt-labs").rglob("*.port"))
                files += list((home / ".punt-labs").rglob("port"))
                for path in files:
                    try:
                        port = int(path.read_text().strip())
                    except (ValueError, OSError):
                        continue
                if port:
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/health", timeout=1
                        ) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        pass
                time.sleep(0.05)
            else:
                result["startup_error"] = f"no ready daemon; exit={daemon.poll()}"
                return result
            result["port"] = port
            cli = (
                [
                    str(binary / "lux"),
                    "scene",
                    "show",
                    "smoke-cli",
                    '{"elements": []}',
                    "--json",
                ]
                if repo == "lux"
                else [str(binary / "quarry"), "find", "smoke query"]
            )
            result["cli_command"] = cli
            result["cli"] = invoke(cli, env, home)
            if switch is None:
                if repo == "lux":
                    try:
                        result["http_mcp"] = http_mcp(port, SECRET)
                    except (OSError, ValueError) as exc:
                        result["http_mcp_error"] = str(exc)
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                    result["concurrent"] = list(
                        pool.map(lambda _: invoke(cli, env, home), range(6))
                    )
                if repo == "quarry":
                    result["mcp"] = stdio(
                        [str(binary / "quarry"), "mcp"],
                        env,
                        home,
                        ("find", {"query": SECRET}),
                        home / "mcp-stderr.txt",
                    )
                if wait_for_export:
                    deadline = time.monotonic() + 75
                    db = home / ".punt-labs" / repo / "telemetry.db"
                    while time.monotonic() < deadline:
                        with sqlite3.connect(db) as conn:
                            rows = conn.execute(
                                "SELECT count(*) FROM metric_points"
                            ).fetchone()[0]
                        if rows:
                            result["periodic_export_before_stop"] = inspect(home, repo)
                            break
                        time.sleep(0.2)
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
                result["forced_shutdown"] = True
            result["daemon_exit"] = daemon.returncode
            result["store"] = inspect(home, repo)
            result["grep"] = grep_private(home, repo)
            result["daemon_output"] = (home / "daemon-output.txt").read_text(
                errors="replace"
            )
            (OUTPUT / (repo + "-daemon-" + (switch or "on") + ".json")).write_text(
                json.dumps(result, indent=2) + "\n"
            )
    return result


if __name__ == "__main__":
    selected = [r for r in sys.argv[2:] if r != "--wait-for-export"]
    for repo in selected or ("lux", "quarry"):
        baseline = flight(repo, wait_for_export="--wait-for-export" in sys.argv)
        sys.stdout.write(
            json.dumps(
                {
                    "repo": repo,
                    "startup_error": baseline.get("startup_error"),
                    "cli": baseline.get("cli"),
                    "counts": baseline["store"].get("counts"),
                    "concurrent_exits": [
                        r["exit"] for r in baseline.get("concurrent", [])
                    ],
                    "daemon_output_tail": baseline["daemon_output"][-2000:],
                }
            )
            + "\n"
        )
        sys.stdout.flush()
        if "startup_error" not in baseline and "--wait-for-export" not in sys.argv:
            for switch in ("PUNT_TELEMETRY", repo.upper() + "_TELEMETRY"):
                off = flight(repo, switch)
                sys.stdout.write(
                    json.dumps(
                        {
                            "repo": repo,
                            "switch": switch,
                            "db_exists": off["store"]["db_exists"],
                            "same_cli_output": off.get("cli") == baseline.get("cli"),
                        }
                    )
                    + "\n"
                )
                sys.stdout.flush()
