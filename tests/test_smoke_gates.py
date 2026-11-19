"""Mutate successful observations to prove the publish gate fails closed."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from smoke import gates
from smoke.evidence import save

type Observation = dict[str, Any]
SECRET = "SMOKE-SECRET-4242 /home/ubuntu/private/path"
SCRIPTS = Path(__file__).resolve().parents[1] / "smoke"


def live_modes(tool: str = "smoketool") -> Observation:
    return {
        ".": "0o700",
        tool: "0o700",
        f"{tool}/logs": "0o700",
        "telemetry.salt": "0o600",
        f"{tool}/telemetry.db": "0o600",
        f"{tool}/telemetry.db-wal": "0o600",
        f"{tool}/telemetry.db-shm": "0o600",
        f"{tool}/logs/{tool}.log": "0o600",
    }


def store(tool: str = "smoketool") -> Observation:
    return {
        "db_exists": True,
        "integrity": "ok",
        "journal_mode": "wal",
        "auto_vacuum": 2,
        "secret_hits": [],
        "log_secret_hits": [],
        "modes": {
            ".punt-labs" if p == "." else ".punt-labs/" + p: v
            for p, v in live_modes(tool).items()
        },
        "logs": {
            tool + ".log": (
                "URL redaction probe https://example.invalid/…(segments=1)\n"
                "Telemetry dropped: unknown_attribute"
            )
        },
        "counts": {"meta": 1, "spans": 1, "span_events": 1, "metric_points": 2},
    }


def execution(data: Observation | None = None) -> Observation:
    return {
        "exit": 0,
        "stdout": json.dumps(data or {"live_modes": live_modes()}),
        "stderr": "",
    }


@pytest.mark.parametrize("path", list(live_modes()))
def test_package_rejects_each_insecure_live_mode(path: str) -> None:
    observation = live_modes()
    observation[path] = (
        "0o755" if path in (".", "smoketool", "smoketool/logs") else "0o644"
    )
    with pytest.raises(gates.GateFailure, match="mode"):
        gates.package_case("emit", execution({"live_modes": observation}), store())


@pytest.mark.parametrize("path", list(live_modes()))
def test_package_rejects_missing_required_modes(path: str) -> None:
    observation = live_modes()
    del observation[path]
    with pytest.raises(gates.GateFailure, match="mode"):
        gates.package_case("emit", execution({"live_modes": observation}), store())


@pytest.mark.parametrize(
    "key,value",
    [
        ("db_exists", False),
        ("integrity", "corrupt"),
        ("journal_mode", "delete"),
        ("auto_vacuum", 0),
        ("secret_hits", ["db"]),
        ("log_secret_hits", ["log"]),
        ("counts", {}),
    ],
)
def test_package_rejects_store_violations(key: str, value: object) -> None:
    observation = store()
    observation[key] = value
    with pytest.raises(gates.GateFailure):
        gates.package_case("emit", execution(), observation)


@pytest.mark.parametrize(
    "logs",
    [
        {},
        {"a.log": "redaction probe https://example.invalid/raw?token=secret"},
        {
            "a.log": (
                "redaction probe https://example.invalid/…(segments=1) SMOKE-PRIVATE"
            )
        },
    ],
)
def test_url_probe_is_positive_and_redacted(logs: Observation) -> None:
    observation = store()
    observation["logs"] = logs
    with pytest.raises(gates.GateFailure):
        gates.package_case("emit", execution(), observation)


def test_normal_package_and_metadata_only_rejection() -> None:
    gates.package_case("emit", execution(), store())
    observation = store()
    observation["counts"].update(spans=0, span_events=0, metric_points=0)
    gates.package_case("invalid", execution(), observation)
    observation["logs"] = {
        "x.log": observation["logs"]["smoketool.log"].replace(
            "unknown_attribute", "undeclared"
        )
    }
    with pytest.raises(gates.GateFailure):
        gates.package_case("invalid", execution(), observation)


@pytest.mark.parametrize("children", [[0, 1, 0], [0, -7, 0], [0, 0], [], None])
def test_every_fork_child_must_succeed(children: object) -> None:
    observation = store()
    observation["counts"].update(spans=4, span_events=4, metric_points=8)
    gates.package_case(
        "fork",
        execution({"live_modes": live_modes(), "child_exitcodes": [0, 0, 0]}),
        observation,
    )
    with pytest.raises(gates.GateFailure, match="child exits"):
        gates.package_case(
            "fork",
            execution({"live_modes": live_modes(), "child_exitcodes": children}),
            observation,
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("seeded", 0),
        ("rows_after", 1),
        ("freelist_after", 1),
        ("pages_after", 282),
        ("pages_after", 0),
    ],
)
def test_idle_requires_expiry_and_reclamation(key: str, value: int) -> None:
    data = {
        "live_modes": live_modes(),
        "seeded": 128,
        "rows_after": 0,
        "freelist_after": 0,
        "pages_before": 282,
        "pages_after": 10,
    }
    observation = store()
    observation["counts"].update(spans=0, span_events=0, metric_points=0)
    gates.package_case("idle", execution(data), observation)
    data[key] = value
    with pytest.raises(gates.GateFailure):
        gates.package_case("idle", execution(data), observation)


@pytest.mark.parametrize(
    "bad",
    [
        {"exit": 1},
        {"exit": "timeout"},
        {"exit": 0, "stdout": "not JSON"},
        {"exit": 0, "stdout": "[]"},
    ],
)
def test_package_execution_failures(bad: Observation) -> None:
    with pytest.raises(gates.GateFailure):
        gates.package_case("emit", bad, store())


@pytest.mark.parametrize(
    "data",
    [
        {"db_exists": True, "extra_threads_before_shutdown": []},
        {"db_exists": False, "extra_threads_before_shutdown": ["worker"]},
        {},
    ],
)
def test_off_requires_no_store_or_threads(data: Observation) -> None:
    gates.package_off(
        execution({"db_exists": False, "extra_threads_before_shutdown": []})
    )
    with pytest.raises(gates.GateFailure):
        gates.package_off(execution(data))


@pytest.mark.parametrize("failure", ["exit", "count", "rows", "integrity"])
def test_package_concurrency_gate(failure: str) -> None:
    executions = [execution() for _ in range(8)]
    observation = store()
    observation["counts"].update(spans=8, span_events=8, metric_points=16)
    gates.package_concurrency(executions, observation)
    if failure == "exit":
        executions[0]["exit"] = -7
    elif failure == "count":
        executions.pop()
    elif failure == "rows":
        observation["counts"]["metric_points"] = 15
    else:
        observation["integrity"] = "bad"
    with pytest.raises(gates.GateFailure):
        gates.package_concurrency(executions, observation)


def otlp_data() -> Observation:
    return {
        "db_exists": False,
        "requests": [
            {
                "path": "/v1/traces",
                "size": 322,
                "headers": {"Content-Type": "application/x-protobuf"},
                "decoded": {
                    "resourceSpans": [
                        {"scopeSpans": [{"spans": [{"name": "smoketool.cli.run"}]}]}
                    ]
                },
            },
            {
                "path": "/v1/metrics",
                "size": 536,
                "headers": {"Content-Type": "application/x-protobuf"},
                "decoded": {
                    "resourceMetrics": [
                        {
                            "scopeMetrics": [
                                {
                                    "metrics": [
                                        {
                                            "name": "smoketool.cli.count",
                                            "sum": {"dataPoints": [{"asDouble": 2}]},
                                        },
                                        {
                                            "name": "smoketool.cli.duration",
                                            "histogram": {
                                                "dataPoints": [{"count": "1"}]
                                            },
                                        },
                                    ]
                                }
                            ]
                        }
                    ]
                },
            },
        ],
    }


@pytest.mark.parametrize(
    "failure",
    [
        "span",
        "counter",
        "histogram",
        "type",
        "ambient",
        "compression",
        "database",
        "endpoint",
        "empty",
    ],
)
def test_otlp_checks_decoded_signals(failure: str) -> None:
    data = otlp_data()
    gates.otlp(execution(data))
    trace, metric = data["requests"]
    if failure == "span":
        trace["decoded"] = {}
    elif failure in ("counter", "histogram"):
        metric["decoded"]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"].pop(
            0 if failure == "counter" else 1
        )
    elif failure == "type":
        trace["headers"]["Content-Type"] = "text/plain"
    elif failure == "ambient":
        metric["headers"]["X-Smoke-Secret"] = "ambient"
    elif failure == "compression":
        trace["headers"]["Content-Encoding"] = "gzip"
    elif failure == "database":
        data["db_exists"] = True
    elif failure == "endpoint":
        trace["path"] = "/wrong"
    else:
        metric["size"] = 0
    with pytest.raises(gates.GateFailure):
        gates.otlp(execution(data))


def rpc(request_id: int, result: Observation) -> Observation:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def exchange(repo: str = "lux") -> Observation:
    tool = {
        "lux": "scene_show",
        "quarry": "find",
        "z-spec": "check",
        "biff": "finger",
        "vox": "speak",
    }[repo]
    text = {
        "lux": "shown:" + SECRET,
        "quarry": f'No results for "{SECRET}"',
        "z-spec": json.dumps({"ok": False, "error": "Spec file not found: " + SECRET}),
        "vox": json.dumps({"error": "Invalid mode '" + SECRET + "'. Use y/n."}),
        "biff": "Login: SMOKE-SECRET-4242\nNever logged in.",
    }[repo]
    return {
        "initialize": rpc(
            1, {"protocolVersion": "2024-11-05", "serverInfo": {"name": repo}}
        ),
        "tools": rpc(2, {"tools": [{"name": tool}]}),
        "call": rpc(
            3,
            {
                "isError": False,
                "content": [{"type": "text", "text": text}],
                "structuredContent": {"result": text},
            },
        ),
    }


@pytest.mark.parametrize(
    "repo,tool",
    [
        ("lux", "scene_show"),
        ("quarry", "find"),
        ("z-spec", "check"),
        ("biff", "finger"),
        ("vox", "speak"),
    ],
)
@pytest.mark.parametrize(
    "failure",
    ["rpc", "listing", "result", "is_error", "structured", "id", "initialize"],
)
def test_mcp_rejects_protocol_and_unexpected_tool_results(
    repo: str, tool: str, failure: str
) -> None:
    data = exchange(repo)
    gates.mcp_exchange(data, repo, tool)
    if failure == "rpc":
        data["call"]["error"] = {"code": -1}
    elif failure == "listing":
        data["tools"]["result"]["tools"] = []
    elif failure == "result":
        data["call"]["result"]["content"][0]["text"] = "tool refused"
    elif failure == "is_error":
        data["call"]["result"]["isError"] = True
    elif failure == "structured":
        data["call"]["result"]["structuredContent"] = {}
    elif failure == "id":
        data["call"]["id"] = 42
    else:
        data["initialize"]["result"] = {}
    with pytest.raises(gates.GateFailure):
        gates.mcp_exchange(data, repo, tool)


def http_transcript() -> list[Observation]:
    return [
        {
            "send": {"id": i},
            "status": 200,
            "content_type": "text/event-stream",
            "body": "event: message\r\ndata: " + json.dumps(message) + "\r\n\r\n",
        }
        for i, message in enumerate(exchange().values(), 1)
    ]


@pytest.mark.parametrize(
    "failure",
    ["json", "id", "rpc", "listing", "result", "status", "content_type", "empty"],
)
def test_http_mcp_parses_and_checks_rpc_bodies(failure: str) -> None:
    data = http_transcript()
    gates.http_exchange(data)
    call = data[-1]
    message = exchange()["call"]
    if failure == "json":
        call["body"] = "data: not JSON\n\n"
    elif failure == "status":
        call["status"] = 500
    elif failure == "content_type":
        call["content_type"] = "text/plain"
    elif failure == "empty":
        data.clear()
    elif failure == "listing":
        data[1]["body"] = "data: " + json.dumps(rpc(2, {"tools": []})) + "\n\n"
    else:
        if failure == "id":
            message["id"] = 90
        elif failure == "rpc":
            message["error"] = {"code": -1}
        else:
            message["result"] = {}
        call["content_type"] = "application/json"
        call["body"] = json.dumps(message)
    with pytest.raises(gates.GateFailure):
        gates.http_exchange(data)


def test_http_json_response_and_notification_acknowledgement() -> None:
    data = http_transcript()
    data[0].update(
        body=json.dumps(exchange()["initialize"]),
        content_type="application/json; charset=utf-8",
    )
    data.insert(
        1, {"send": {"method": "notifications/initialized"}, "status": 202, "body": ""}
    )
    gates.http_exchange(data)
    data[1]["body"] = '{"error": "rejected"}'
    with pytest.raises(gates.GateFailure, match="acknowledgement"):
        gates.http_exchange(data)


def tool_result() -> Observation:
    result: Observation = {
        "cli": {"exit": 0},
        "log_probe": {"exit": 0},
        "store": store("vox"),
        "redaction_store": store("vox"),
        "off": [
            {"execution": {"exit": 0}, "no_db": True, "same_output": True}
            for _ in range(2)
        ],
        "concurrency": {
            "executions": [{"exit": 0} for _ in range(6)],
            "store": store("vox"),
        },
        "mcp": {
            "execution": exchange("vox") | {"exit": 0},
            "store": store("vox"),
            "grep": {"exit": 1},
        },
    }
    result["concurrency"]["store"]["counts"]["spans"] = 6
    result["mcp"]["store"]["signals"] = [
        ["vox.mcp.tool", json.dumps({"tool": "speak", "outcome": "error"})]
    ]
    return result


@pytest.mark.parametrize(
    "failure",
    [
        "baseline",
        "rows",
        "log_exit",
        "redaction",
        "concurrent",
        "concurrent_rows",
        "off_exit",
        "off_db",
        "off_output",
        "off_coverage",
        "mcp_exit",
        "mcp_protocol",
        "mcp_marker",
        "mcp_signal",
    ],
)
def test_adopter_observations_cannot_only_be_recorded(failure: str) -> None:
    result = tool_result()
    gates.tool_checks(result, "vox")
    if failure == "baseline":
        result["cli"]["exit"] = 1
    elif failure == "rows":
        result["store"]["counts"]["spans"] = 0
    elif failure == "log_exit":
        result["log_probe"]["exit"] = 1
    elif failure == "redaction":
        result["redaction_store"]["logs"] = {}
    elif failure == "concurrent":
        result["concurrency"]["executions"][0]["exit"] = -7
    elif failure == "concurrent_rows":
        result["concurrency"]["store"]["counts"]["spans"] = 5
    elif failure.startswith("off_"):
        if failure == "off_coverage":
            result["off"].pop()
        elif failure == "off_exit":
            result["off"][0]["execution"]["exit"] = 1
        else:
            result["off"][0]["no_db" if failure == "off_db" else "same_output"] = False
    elif failure == "mcp_exit":
        result["mcp"]["execution"]["exit"] = 1
    elif failure == "mcp_protocol":
        result["mcp"]["execution"]["error"] = "refused"
    elif failure == "mcp_marker":
        result["mcp"]["grep"]["exit"] = 0
    else:
        result["mcp"]["store"]["signals"] = []
    with pytest.raises(gates.GateFailure):
        gates.tool_checks(result, "vox")


def daemon_result() -> Observation:
    result: Observation = {
        "port": 1234,
        "daemon_exit": -15,
        "cli": {"exit": 0, "stdout": "same"},
        "concurrent": [{"exit": 0} for _ in range(6)],
        "store": store("lux"),
        "grep": {"exit": 1},
        "http_mcp": http_transcript(),
    }
    result["store"]["counts"]["metric_points"] = 1
    result["store"]["metrics"] = [
        [
            "lux.hub.operation_duration",
            8,
            json.dumps({"operation": "render", "outcome": "ok"}),
        ]
    ]
    return result


@pytest.mark.parametrize(
    "failure",
    [
        "startup",
        "shutdown",
        "cli",
        "concurrent",
        "rows",
        "render_count",
        "marker",
        "http",
    ],
)
def test_daemon_gate_rejects_loss_including_lux_short_stop(failure: str) -> None:
    result = daemon_result()
    gates.daemon_checks(result, "lux")
    if failure == "startup":
        result["startup_error"] = "not ready"
    elif failure == "shutdown":
        result["forced_shutdown"] = True
    elif failure == "cli":
        result["cli"]["exit"] = 1
    elif failure == "concurrent":
        result["concurrent"][0]["exit"] = 1
    elif failure == "rows":
        result["store"]["counts"]["metric_points"] = 0
    elif failure == "render_count":
        result["store"]["metrics"][0][1] = 7
    elif failure == "marker":
        result["grep"]["exit"] = 0
    else:
        result["http_mcp"] = []
    with pytest.raises(gates.GateFailure):
        gates.daemon_checks(result, "lux")


@pytest.mark.parametrize("failure", ["db", "output", "exit", "startup"])
def test_daemon_off_switch_is_enforced(failure: str) -> None:
    baseline = daemon_result()
    off = copy.deepcopy(baseline)
    off["store"] = {"db_exists": False}
    gates.daemon_checks(off, "lux", baseline)
    if failure == "db":
        off["store"]["db_exists"] = True
    elif failure == "output":
        off["cli"]["stdout"] = "changed"
    elif failure == "exit":
        off["cli"]["exit"] = 1
    else:
        off["startup_error"] = "failed"
    with pytest.raises(gates.GateFailure):
        gates.daemon_checks(off, "lux", baseline)


@pytest.mark.parametrize("count", [0, 2])
def test_build_requires_exactly_one_new_wheel(tmp_path: Path, count: int) -> None:
    for index in range(count):
        (tmp_path / f"punt_observe-{index}-py3-none-any.whl").touch()
    with pytest.raises(gates.GateFailure, match="newly built wheel"):
        gates.built_wheel(tmp_path)


def test_installer_ignores_stale_dist_wheels(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/punt_observe-old.whl").touch()
    (tmp_path / "uv").write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, sys
from pathlib import Path
with Path(os.environ["COMMANDS"]).open("a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1] == "build":
    (Path(sys.argv[-1]) / "punt_observe-new.whl").touch()
"""
    )
    (tmp_path / "uv").chmod(0o700)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "install_wheel.py"), "isolated-venv"],
        cwd=tmp_path,
        env=os.environ
        | {
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "COMMANDS": str(tmp_path / "commands.jsonl"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    build, install = [
        json.loads(line)
        for line in (tmp_path / "commands.jsonl").read_text().splitlines()
    ]
    assert install[-1] == str(Path(build[-1]) / "punt_observe-new.whl")
    assert "dist/punt_observe-old.whl" not in " ".join(install)


def test_provenance_creates_fresh_parent(tmp_path: Path) -> None:
    path = tmp_path / "fresh/tool-results/provenance.json"
    save(path, {"tool": "pin"})
    assert json.loads(path.read_text()) == {"tool": "pin"}


def test_verdict_reports_the_failing_check_and_empty_observations() -> None:
    assert gates.verdict(lambda: gates.package_case("emit", execution(), store())) == {
        "pass": True
    }
    assert gates.verdict(lambda: gates.package_case("emit", {}, {})) == {
        "pass": False,
        "failure": "package/emit: process exit",
    }
    assert gates.verdict(lambda: gates.tool_checks({}, "vox"))["pass"] is False
    assert gates.verdict(lambda: gates.daemon_checks({}, "lux"))["pass"] is False
