"""Exercise installed adopted tools with isolated homes and stdio JSON-RPC."""

from __future__ import annotations

import concurrent.futures
import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from gates import mcp_exchange, rpc_result, tool_checks, verdict
from inspect_store import inspect

ROOT = Path(sys.argv[1]).resolve()
OUTPUT = ROOT / "tool-results"
OUTPUT.mkdir(exist_ok=True)
SECRET = "SMOKE-SECRET-4242 /home/ubuntu/private/path"
TOOLS = {
    "punt-kit": ("punt", ["version"], None),
    "z-spec": ("z-spec", ["disable"], ("check", {"file": SECRET})),
    "lux": ("lux", ["version"], ("scene_show", {"scene_id": SECRET, "elements": []})),
    "biff": (
        "biff",
        ["version"],
        (
            "finger",
            {
                "user": SECRET,
                "_caller": {"session_id": "smoke-session", "agent_id": "smoke-agent"},
            },
        ),
    ),
    "quarry": ("quarry", ["version"], ("find", {"query": SECRET})),
    "vox": ("vox", ["version"], ("speak", {"mode": SECRET})),
}


def fresh(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=label + "-", dir=OUTPUT))


def environment(home: Path, repo: str) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": f"{ROOT / repo / '.venv/bin'}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "HF_HUB_OFFLINE": "1",
        "HF_HOME": str(ROOT / "model-cache"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }


def invoke(
    command: list[str], env: dict[str, str], cwd: Path, timeout: int = 45
) -> dict:
    try:
        p = subprocess.run(
            command, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired:
        return {"exit": "timeout"}


def stdio(
    command: list[str],
    env: dict[str, str],
    cwd: Path,
    call: tuple[str, dict],
    stderr: Path,
) -> dict:
    transcript = []
    with stderr.open("w") as errors:
        p = subprocess.Popen(
            command,
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        assert p.stdin is not None and p.stdout is not None
        poll = selectors.DefaultSelector()
        poll.register(p.stdout, selectors.EVENT_READ)
        buffer = b""

        def send(message: dict) -> None:
            transcript.append({"send": message})
            p.stdin.write(json.dumps(message).encode() + b"\n")
            p.stdin.flush()

        def receive(request_id: int) -> dict:
            nonlocal buffer
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        message = json.loads(line)
                    except ValueError:
                        transcript.append(
                            {"non_json_stdout": line.decode(errors="replace")}
                        )
                        continue
                    transcript.append({"receive": message})
                    if message.get("id") == request_id:
                        rpc_result(message, request_id, "stdio/mcp")
                        return message
                if poll.select(max(0, deadline - time.monotonic())):
                    chunk = os.read(p.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError(f"server EOF, exit={p.poll()}")
                    buffer += chunk
            raise TimeoutError(f"request {request_id} timed out")

        result = {}
        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "telemetry-smoke", "version": "1"},
                    },
                }
            )
            result["initialize"] = receive(1)
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            listing = receive(2)
            result["tools"] = listing
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": call[0], "arguments": call[1]},
                }
            )
            result["call"] = receive(3)
            repo = {
                "check": "z-spec",
                "finger": "biff",
                "scene_show": "lux",
                "find": "quarry",
                "speak": "vox",
            }[call[0]]
            mcp_exchange(result, repo, call[0])
        except (TimeoutError, RuntimeError, BrokenPipeError) as exc:
            result["error"] = str(exc)
        finally:
            p.stdin.close()
            try:
                p.wait(timeout=12)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                result["forced_shutdown"] = True
            poll.close()
        result.update(
            exit=p.returncode,
            transcript=transcript,
            stderr=stderr.read_text(errors="replace"),
        )
        return result


def grep_private(home: Path, tool: str) -> dict:
    root = home / ".punt-labs" / tool
    files = [
        str(p)
        for p in root.rglob("*")
        if p.is_file() and (p.name.startswith("telemetry.db") or ".log" in p.name)
    ]
    if not files:
        return {"exit": None, "files": [], "note": "no db or log to scan"}
    p = subprocess.run(
        [
            "grep",
            "-a",
            "-l",
            "-F",
            "-e",
            "SMOKE-SECRET-4242",
            "-e",
            "/home/ubuntu/private/path",
            *files,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return {"exit": p.returncode, "files": files, "hits": p.stdout.splitlines()}


def flight(repo: str) -> dict:
    binary, args, call = TOOLS[repo]
    tool = "punt" if repo == "punt-kit" else repo
    executable = str(ROOT / repo / ".venv/bin" / binary)
    cwd = fresh(repo + "-repo")
    subprocess.run(["git", "init", "-q", str(cwd)], check=True)
    home = fresh(repo + "-cli")
    baseline = invoke([executable, *args], environment(home, repo), cwd)
    result = {
        "command": [binary, *args],
        "home": str(home),
        "cli": baseline,
        "store": inspect(home, tool),
        "off": [],
    }
    result["log_probe"] = invoke(
        [
            str(ROOT / repo / ".venv/bin/python"),
            str(Path(__file__).with_name("log_probe.py")),
            repo,
        ],
        environment(home, repo),
        cwd,
    )
    result["redaction_store"] = inspect(home, tool)
    for switch in ("PUNT_TELEMETRY", tool.upper().replace("-", "_") + "_TELEMETRY"):
        off_home = fresh(repo + "-off")
        execution = invoke(
            [executable, *args], environment(off_home, repo) | {switch: "off"}, cwd
        )
        result["off"].append(
            {
                "switch": switch,
                "execution": execution,
                "no_db": not (off_home / ".punt-labs" / tool / "telemetry.db").exists(),
                "same_output": execution == baseline,
            }
        )
    concurrent_home = fresh(repo + "-concurrent")
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        executions = list(
            pool.map(
                lambda _: invoke(
                    [executable, *args], environment(concurrent_home, repo), cwd
                ),
                range(6),
            )
        )
    result["concurrency"] = {
        "executions": executions,
        "store": inspect(concurrent_home, tool),
        "home": str(concurrent_home),
    }
    if call is not None and repo != "quarry":
        mcp_home = fresh(repo + "-mcp")
        if repo in ("z-spec", "biff"):
            result["mcp_setup"] = invoke(
                [executable, "enable"], environment(mcp_home, repo), cwd
            )
        command = [executable, "mcp"]
        if repo == "lux":
            command = [
                str(ROOT / repo / ".venv/bin/python"),
                str(Path(__file__).with_name("lux_stdio.py")),
            ]
        if repo == "biff":
            command += ["--user", "smoke", "--relay-url", ""]
        result["mcp"] = {
            "home": str(mcp_home),
            "execution": stdio(
                command, environment(mcp_home, repo), cwd, call, mcp_home / "stderr.txt"
            ),
            "store": inspect(mcp_home, tool),
            "grep": grep_private(mcp_home, tool),
        }
    else:
        result["mcp"] = {
            "note": "quarry MCP is required by the daemon flight; punt-kit has no MCP"
        }
    if repo in ("lux", "quarry"):
        result["daemon"] = invoke(
            [
                sys.executable,
                str(Path(__file__).with_name("run_daemons.py")),
                str(ROOT),
                repo,
            ],
            os.environ.copy(),
            cwd,
            timeout=180,
        )
    result.update(verdict(lambda: tool_checks(result, repo)))
    (OUTPUT / (repo + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    return {
        "repo": repo,
        "pass": result["pass"],
        "failure": result.get("failure"),
        "cli_exit": baseline["exit"],
        "counts": result["store"].get("counts"),
        "off": [
            {k: v for k, v in r.items() if k != "execution"} for r in result["off"]
        ],
        "concurrent_exits": [r["exit"] for r in executions],
        "mcp_error": result["mcp"].get("execution", {}).get("error"),
    }


if __name__ == "__main__":
    failed = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        for summary in executor.map(flight, sys.argv[2:] or TOOLS):
            sys.stdout.write(json.dumps(summary) + "\n")
            sys.stdout.flush()
            if not summary["pass"]:
                failed = True
                sys.stderr.write(f"FAIL {summary['repo']}: {summary['failure']}\n")
    sys.exit(int(failed))
