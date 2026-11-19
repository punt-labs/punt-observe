"""Fail-closed acceptance checks shared by the installed-artifact runners."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

type Observation = dict[str, Any]


class GateFailure(RuntimeError):
    """Name the acceptance check that rejected an observation."""


def verdict(check: Callable[[], None]) -> Observation:
    try:
        check()
    except (
        GateFailure,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
    ) as exc:
        return {"pass": False, "failure": str(exc)}
    return {"pass": True}


def require(condition: object, label: str) -> None:
    if not condition:
        raise GateFailure(label)


def built_wheel(directory: Path) -> Path:
    wheels = list(directory.glob("*.whl"))
    require(len(wheels) == 1, "build: expected exactly one newly built wheel")
    return wheels[0]


def execution_data(execution: Observation, label: str) -> Observation:
    require(execution.get("exit") == 0, label + ": process exit")
    try:
        data = json.loads(execution.get("stdout", ""))
    except (ValueError, TypeError) as exc:
        raise GateFailure(label + ": JSON output") from exc
    require(isinstance(data, dict), label + ": JSON object")
    return cast(Observation, data)


def modes(values: Observation, tool: str, label: str, *, live: bool = False) -> None:
    prefix = "" if live else ".punt-labs/"
    root = "." if live else ".punt-labs"
    directories = {root, prefix + tool, f"{prefix}{tool}/logs"}
    files = {
        f"{prefix}{tool}/telemetry.db",
        f"{prefix}{tool}/logs/{tool}.log",
    }
    if live:
        files.update(f"{tool}/telemetry.db-{suffix}" for suffix in ("wal", "shm"))
    for path in directories | files:
        require(path in values, f"{label}: missing mode {path}")
    for path, mode in values.items():
        if path in directories:
            require(mode == "0o700", f"{label}: directory mode {path}")
        elif (
            live
            or path == prefix + "telemetry.salt"
            or path in files
            or path.startswith(
                (f"{prefix}{tool}/logs/", f"{prefix}{tool}/telemetry.db-")
            )
        ):
            require(mode == "0o600", f"{label}: file mode {path}")


def store_checks(store: Observation, tool: str, label: str) -> None:
    require(store.get("db_exists") is True, label + ": database missing")
    require(store.get("integrity") == "ok", label + ": integrity_check")
    require(store.get("journal_mode") == "wal", label + ": WAL mode")
    require(store.get("auto_vacuum") == 2, label + ": incremental vacuum")
    require(store.get("secret_hits") == [], label + ": private marker leak")
    require(store.get("log_secret_hits") == [], label + ": log marker leak")
    modes(store.get("modes", {}), tool, label + "/modes")


def redaction(store: Observation, label: str) -> None:
    require(store.get("log_secret_hits") == [], label + ": log marker leak")
    logs = "\n".join(store.get("logs", {}).values())
    require("redaction probe" in logs, label + ": redaction probe missing")
    require("https://example.invalid/…(segments=" in logs, label + ": URL redaction")
    for raw in (
        "SMOKE-PRIVATE",
        "SMOKE-SECRET-4242",
        "/home/ubuntu/private/path",
        "?token=",
    ):
        require(raw not in logs, label + ": raw log value")


def package_case(mode: str, execution: Observation, store: Observation) -> None:
    label = "package/" + mode
    data = execution_data(execution, label)
    store_checks(store, "smoketool", label)
    modes(data.get("live_modes", {}), "smoketool", label + "/live_modes", live=True)
    if mode in ("emit", "fork"):
        require(
            data.get("live_modes", {}).get("telemetry.salt") == "0o600",
            label + ": salt mode",
        )
    expected = {"meta": 1, "spans": 1, "span_events": 1, "metric_points": 2}
    if mode in ("invalid", "idle"):
        expected.update(spans=0, span_events=0, metric_points=0)
    elif mode == "fork":
        expected.update(spans=4, span_events=4, metric_points=8)
        require(data.get("child_exitcodes") == [0, 0, 0], label + ": child exits")
    require(store.get("counts") == expected, label + ": stored row counts")
    redaction(store, label)
    if mode == "invalid":
        logs = "\n".join(store.get("logs", {}).values())
        require("Telemetry dropped: unknown_attribute" in logs, label + ": diagnostic")
        require("undeclared" not in logs, label + ": rejected attribute in diagnostic")
    if mode == "idle":
        require(data.get("seeded") == 128, label + ": seeded rows")
        require(data.get("rows_after") == 0, label + ": expired rows")
        require(data.get("freelist_after") == 0, label + ": freelist not reclaimed")
        before, after = data.get("pages_before", 0), data.get("pages_after", 0)
        require(0 < after < before, label + ": pages not reclaimed")


def package_off(execution: Observation) -> None:
    data = execution_data(execution, "package/off")
    require(data.get("db_exists") is False, "package/off: database created")
    require(
        data.get("extra_threads_before_shutdown") == [], "package/off: extra threads"
    )


def package_concurrency(executions: list[Observation], store: Observation) -> None:
    require(len(executions) == 8, "package/concurrency: process count")
    for execution in executions:
        data = execution_data(execution, "package/concurrency")
        modes(data.get("live_modes", {}), "smoketool", "concurrency/modes", live=True)
        require(
            data.get("live_modes", {}).get("telemetry.salt") == "0o600",
            "concurrency: salt mode",
        )
    store_checks(store, "smoketool", "package/concurrency")
    require(
        store.get("counts")
        == {"meta": 1, "spans": 8, "span_events": 8, "metric_points": 16},
        "package/concurrency: stored row counts",
    )
    redaction(store, "package/concurrency")


def otlp(execution: Observation) -> None:
    data = execution_data(execution, "package/otlp")
    require(data.get("db_exists") is False, "package/otlp: local database")
    requests = data.get("requests", [])
    require(
        {r.get("path") for r in requests} == {"/v1/traces", "/v1/metrics"},
        "package/otlp: endpoints",
    )
    spans: set[str] = set()
    metrics: Observation = {}
    for request in requests:
        headers = {k.lower(): v for k, v in request.get("headers", {}).items()}
        require("x-smoke-secret" not in headers, "package/otlp: ambient header")
        require("content-encoding" not in headers, "package/otlp: ambient compression")
        require(
            headers.get("content-type") == "application/x-protobuf",
            "otlp: content type",
        )
        require(request.get("size", 0) > 0, "package/otlp: empty body")
        decoded = request.get("decoded", {})
        for resource in decoded.get("resourceSpans", []):
            for scope in resource.get("scopeSpans", []):
                spans.update(span.get("name", "") for span in scope.get("spans", []))
        for resource in decoded.get("resourceMetrics", []):
            for scope in resource.get("scopeMetrics", []):
                metrics.update({m.get("name"): m for m in scope.get("metrics", [])})
    require("smoketool.cli.run" in spans, "package/otlp: emitted span missing")
    for name, kind in (("count", "sum"), ("duration", "histogram")):
        require(
            metrics.get("smoketool.cli." + name, {}).get(kind, {}).get("dataPoints"),
            "package/otlp: emitted " + name + " missing",
        )


def rpc_result(message: Observation, request_id: int, label: str) -> Observation:
    require(message.get("jsonrpc") == "2.0", label + ": JSON-RPC version")
    require(message.get("id") == request_id, label + ": response id")
    require("error" not in message, label + ": JSON-RPC error")
    require(isinstance(message.get("result"), dict), label + ": result object")
    return cast(Observation, message["result"])


def mcp_exchange(exchange: Observation, repo: str, tool: str) -> None:
    label = repo + "/mcp"
    secret = "SMOKE-SECRET-4242 /home/ubuntu/private/path"
    init = rpc_result(exchange.get("initialize", {}), 1, label + "/initialize")
    require(init.get("protocolVersion"), label + ": protocol version")
    require(init.get("serverInfo", {}).get("name"), label + ": server info")
    listing = rpc_result(exchange.get("tools", {}), 2, label + "/list")
    require(
        tool in {t.get("name") for t in listing.get("tools", [])},
        label + ": tool absent",
    )
    result = rpc_result(exchange.get("call", {}), 3, label + "/call")
    require(result.get("isError") is False, label + ": tool error")
    content = result.get("content", [])
    require(
        len(content) == 1 and content[0].get("type") == "text",
        label + ": typed content",
    )
    text = content[0].get("text", "")
    require(
        result.get("structuredContent", {}).get("result") == text,
        label + ": structured result",
    )
    if repo in ("z-spec", "vox"):
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise GateFailure(label + ": typed error JSON") from exc
        require(isinstance(data, dict), label + ": typed error object")
        expected = "Spec file not found:" if repo == "z-spec" else "Invalid mode '"
        require(
            str(data.get("error", "")).startswith(expected),
            label + ": expected typed error",
        )
        require(secret in data.get("error", ""), label + ": planted argument absent")
        if repo == "z-spec":
            require(data.get("ok") is False, label + ": expected failed check")
    elif repo == "biff":
        require(
            "Login: SMOKE-SECRET-4242" in text and "Never logged in." in text,
            label + ": finger result",
        )
    else:
        expected = "shown:" + secret if repo == "lux" else f'No results for "{secret}"'
        require(text == expected, label + ": unexpected tool result")


def http_response(body: str, content_type: str, request_id: int) -> Observation:
    try:
        if content_type.split(";")[0] == "text/event-stream":
            messages = [
                json.loads(
                    "\n".join(
                        line[5:].strip()
                        for line in event.splitlines()
                        if line.startswith("data:")
                    )
                )
                for event in body.replace("\r\n", "\n").split("\n\n")
                if any(line.startswith("data:") for line in event.splitlines())
            ]
        else:
            require(
                content_type.split(";")[0] == "application/json",
                "http/mcp: content type",
            )
            messages = [json.loads(body)]
    except (ValueError, TypeError) as exc:
        raise GateFailure("http/mcp: invalid JSON body") from exc
    matches = [m for m in messages if isinstance(m, dict) and m.get("id") == request_id]
    require(len(matches) == 1, "http/mcp: matching response id")
    rpc_result(matches[0], request_id, "http/mcp")
    return cast(Observation, matches[0])


def http_exchange(transcript: list[Observation]) -> None:
    exchange = {}
    for message in transcript:
        request_id = message.get("send", {}).get("id")
        if request_id is None:
            require(
                message.get("status") == 202 and not message.get("body"),
                "http/mcp: notification acknowledgement",
            )
            continue
        require(message.get("status") == 200, "http/mcp: HTTP status")
        key = {1: "initialize", 2: "tools", 3: "call"}.get(request_id)
        require(key is not None and key not in exchange, "http/mcp: request sequence")
        exchange[cast(str, key)] = http_response(
            message.get("body", ""), message.get("content_type", ""), request_id
        )
    mcp_exchange(exchange, "lux", "scene_show")


def typed_signal(
    store: Observation, name: str, attributes: Observation, label: str
) -> None:
    for signal, encoded in store.get("signals", []):
        if signal == name:
            values = json.loads(encoded)
            if all(values.get(k) == v for k, v in attributes.items()):
                return
    raise GateFailure(label + ": expected typed signal " + name)


def mcp_checks(mcp: Observation, repo: str) -> None:
    execution = mcp.get("execution", {})
    require(
        execution.get("exit") == 0 and not execution.get("forced_shutdown"),
        repo + "/mcp: process exit",
    )
    require(not execution.get("error"), repo + "/mcp: protocol failure")
    tool = {
        "z-spec": "check",
        "biff": "finger",
        "vox": "speak",
        "lux": "scene_show",
        "quarry": "find",
    }[repo]
    mcp_exchange(execution, repo, tool)
    store = mcp.get("store", {})
    store_checks(store, repo, repo + "/mcp")
    require(mcp.get("grep", {}).get("exit") == 1, repo + "/mcp: marker scan")
    if repo == "lux":
        require(
            any(
                name == "lux.hub.operation_duration"
                and count >= 1
                and json.loads(attrs) == {"operation": "render", "outcome": "ok"}
                for name, count, attrs in store.get("metrics", [])
            ),
            "lux/mcp: render metric",
        )
    else:
        signals: dict[str, tuple[str, Observation]] = {
            "z-spec": (
                "z-spec.mcp.check",
                {"outcome": "failed", "failure": "spec_not_found"},
            ),
            "biff": ("biff.mcp.call", {"outcome": "ok", "tool": "finger"}),
            "vox": ("vox.mcp.tool", {"outcome": "error", "tool": "speak"}),
            "quarry": ("quarry.daemon.search", {"surface": "mcp", "result_count": 0}),
        }
        name, attrs = signals[repo]
        typed_signal(store, name, attrs, repo + "/mcp")


def tool_checks(result: Observation, repo: str) -> None:
    tool = "punt" if repo == "punt-kit" else repo
    label = repo + "/cli"
    require(result.get("cli", {}).get("exit") == 0, label + ": process exit")
    require(result.get("log_probe", {}).get("exit") == 0, label + ": log probe exit")
    redaction(result.get("redaction_store", {}), label)
    for off in result.get("off", []):
        require(off.get("execution", {}).get("exit") == 0, label + ": off process exit")
        require(off.get("no_db") is True, label + ": off database created")
        require(off.get("same_output") is True, label + ": off output changed")
    require(len(result.get("off", [])) == 2, label + ": off switch coverage")
    concurrent = result.get("concurrency", {})
    executions = concurrent.get("executions", [])
    require(
        len(executions) == 6 and all(e.get("exit") == 0 for e in executions),
        label + ": concurrent exits",
    )
    if repo not in ("lux", "quarry"):
        for store, count in (
            (result.get("store", {}), 1),
            (concurrent.get("store", {}), 6),
        ):
            store_checks(store, tool, label)
            require(
                store.get("counts", {}).get("spans") == count, label + ": stored rows"
            )
        store_checks(result.get("redaction_store", {}), tool, label + "/redaction")
    if repo not in ("punt-kit", "quarry"):
        if repo in ("z-spec", "biff"):
            require(
                result.get("mcp_setup", {}).get("exit") == 0, repo + "/mcp: setup exit"
            )
        mcp_checks(result.get("mcp", {}), repo)
    if repo in ("lux", "quarry"):
        require(
            result.get("daemon", {}).get("exit") == 0,
            repo
            + ": daemon gate failed: "
            + result.get("daemon", {}).get("stderr", "missing result").strip(),
        )


def daemon_checks(
    result: Observation, repo: str, baseline: Observation | None = None
) -> None:
    label = repo + "/daemon"
    require(not result.get("startup_error") and result.get("port"), label + ": startup")
    require(
        not result.get("forced_shutdown") and result.get("daemon_exit") in (0, -15),
        label + ": shutdown",
    )
    require(result.get("cli", {}).get("exit") == 0, label + ": CLI exit")
    if baseline is not None:
        require(
            result.get("store", {}).get("db_exists") is False,
            label + ": off database created",
        )
        require(
            result.get("cli") == baseline.get("cli"), label + ": off output changed"
        )
        return
    executions = result.get("concurrent", [])
    require(
        len(executions) == 6 and all(e.get("exit") == 0 for e in executions),
        label + ": concurrent exits",
    )
    require(result.get("grep", {}).get("exit") == 1, label + ": marker scan")
    store = result.get("store", {})
    store_checks(store, repo, label)
    if repo == "lux":
        require(not result.get("http_mcp_error"), label + ": HTTP MCP failure")
        http_exchange(result.get("http_mcp", []))
        require(
            store.get("counts", {}).get("metric_points", 0) > 0,
            label + ": missing stored metric points after short-stop",
        )
        require(
            any(
                name == "lux.hub.operation_duration"
                and count == 8
                and json.loads(attrs) == {"operation": "render", "outcome": "ok"}
                for name, count, attrs in store.get("metrics", [])
            ),
            label + ": expected eight renders",
        )
    else:
        require(
            store.get("counts", {}).get("spans") == 8,
            label + ": expected eight searches",
        )
        mcp = {
            "execution": result.get("mcp", {}),
            "store": store,
            "grep": result.get("grep", {}),
        }
        mcp_checks(mcp, repo)
