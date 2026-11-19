"""Speak JSON-RPC to Lux's shipped streamable-HTTP MCP endpoint."""

from __future__ import annotations

import json
import urllib.request


def call(port: int, secret: str) -> list[dict]:
    transcript = []
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "scene_show",
                "arguments": {"scene_id": secret, "elements": []},
            },
        },
    ]
    for message in messages:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp?session_key=smoke",
            data=json.dumps(message).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if session := response.headers.get("Mcp-Session-Id"):
                headers["Mcp-Session-Id"] = session
            body = response.read().decode()
            transcript.append(
                {
                    "send": message,
                    "status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "body": body,
                }
            )
    return transcript
