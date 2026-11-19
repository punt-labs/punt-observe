"""Capture real OTLP protobuf HTTP exports on loopback, not a network collector."""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cli import SCHEMAS, emit
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from punt_observe.telemetry import TelemetryConfig, open_telemetry

requests: list[dict[str, object]] = []


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        path = Path(os.environ["SMOKE_OUTPUT"]) / (self.path.rsplit("/", 1)[-1] + ".pb")
        path.write_bytes(body)
        proto = (
            ExportTraceServiceRequest()
            if self.path == "/v1/traces"
            else ExportMetricsServiceRequest()
        )
        proto.ParseFromString(body)
        requests.append(
            {
                "path": self.path,
                "size": len(body),
                "headers": dict(self.headers),
                "decoded": MessageToDict(proto),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
os.environ.update(
    OTEL_EXPORTER_OTLP_HEADERS="X-Smoke-Secret=ambient-fake-header",
    OTEL_EXPORTER_OTLP_TRACES_HEADERS="X-Smoke-Secret=trace-fake-header",
    OTEL_EXPORTER_OTLP_METRICS_HEADERS="X-Smoke-Secret=metric-fake-header",
    OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:1/ignored",
    OTEL_EXPORTER_OTLP_COMPRESSION="gzip",
)
telemetry = open_telemetry(
    "smoketool",
    SCHEMAS,
    TelemetryConfig(
        exporter="otlp",
        otlp_consent=True,
        otlp_endpoint=f"http://127.0.0.1:{server.server_port}",
    ),
)
emit(telemetry)
telemetry.shutdown(timeout_s=2)
server.shutdown()
server.server_close()
sys.stdout.write(
    json.dumps(
        {
            "requests": requests,
            "db_exists": (Path.home() / ".punt-labs/smoketool/telemetry.db").exists(),
        }
    )
    + "\n"
)
