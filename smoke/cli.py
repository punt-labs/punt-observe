"""Exercise the installed package as a pretend tool, without pytest or mocks."""

from __future__ import annotations

import json
import logging
import multiprocessing
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path

import punt_observe.telemetry
from punt_observe.logging import configure_logging
from punt_observe.telemetry import (
    EnumAttr,
    HashAttr,
    SignalSchema,
    Telemetry,
    TelemetryConfig,
    open_telemetry,
    providers,
)

SECRET = "SMOKE-SECRET-4242 /home/ubuntu/private/path"
SCHEMAS = [
    SignalSchema(
        "smoketool.cli.run",
        1,
        "span",
        {
            "outcome": EnumAttr(("ok", "error")),
            "identity": HashAttr(),
        },
    ),
    SignalSchema(
        "smoketool.cli.event",
        1,
        "event",
        {
            "result": EnumAttr(("ok", "error")),
        },
    ),
    SignalSchema("smoketool.cli.count", 1, "counter"),
    SignalSchema("smoketool.cli.duration", 1, "histogram", unit="ms"),
]


def emit(telemetry: Telemetry) -> None:
    with telemetry.span("smoketool.cli.run", 1, identity=telemetry.hash_id(SECRET)):
        telemetry.event("smoketool.cli.event", 1, result="ok")
    telemetry.count("smoketool.cli.count", 1, n=2)
    telemetry.record("smoketool.cli.duration", 1, 12.5)


def fork_child(telemetry: Telemetry) -> None:
    emit(telemetry)
    telemetry.shutdown(timeout_s=2)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "emit"
    if mode == "idle":
        providers._PRUNE_INTERVAL_S = 0.03
    configure_logging("smoketool", "cli")
    before = {t.ident for t in threading.enumerate()}
    telemetry = open_telemetry(
        "smoketool",
        SCHEMAS,
        TelemetryConfig(
            enabled=mode != "disabled", retention_days=1 if mode == "idle" else 90
        ),
    )
    db = Path.home() / ".punt-labs/smoketool/telemetry.db"
    result: dict[str, object] = {"mode": mode}
    if mode == "invalid":
        telemetry.event("smoketool.cli.event", 1, undeclared=SECRET)
        telemetry.event("smoketool.cli.event", 1, result=SECRET)
    elif mode == "fork":
        children = [
            multiprocessing.get_context("fork").Process(
                target=fork_child,
                args=(telemetry,),
            )
            for _ in range(3)
        ]
        for child in children:
            child.start()
        for child in children:
            child.join(10)
            if child.is_alive():
                child.terminate()
                child.join(2)
                raise RuntimeError("fork child timed out")
        result["child_exitcodes"] = [c.exitcode for c in children]
        emit(telemetry)
    elif mode == "idle":
        with sqlite3.connect(db) as conn:
            conn.executemany(
                "INSERT INTO spans (end_ns, attributes) VALUES (0, ?)",
                [("x" * 8192,)] * 128,
            )
            conn.commit()
            result["seeded"] = conn.execute("SELECT count(*) FROM spans").fetchone()[0]
            result["pages_before"] = conn.execute("PRAGMA page_count").fetchone()[0]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rows = conn.execute("SELECT count(*) FROM spans").fetchone()[0]
                free = conn.execute("PRAGMA freelist_count").fetchone()[0]
                if rows == free == 0:
                    break
                time.sleep(0.02)
            result.update(
                rows_after=rows,
                freelist_after=free,
                pages_after=conn.execute("PRAGMA page_count").fetchone()[0],
            )
    else:
        emit(telemetry)
    result["extra_threads_before_shutdown"] = [
        t.name for t in threading.enumerate() if t.ident not in before
    ]
    root = Path.home() / ".punt-labs"
    result["live_modes"] = {
        str(p.relative_to(root)): oct(stat.S_IMODE(p.stat().st_mode))
        for p in [root, *root.rglob("*")]
        if p.is_file() or p.is_dir()
    }
    telemetry.shutdown(timeout_s=2)
    result["db_exists"] = db.exists()
    result["installed_module"] = str(punt_observe.telemetry.__file__)
    logging.getLogger("smoketool").info(
        "URL redaction probe https://example.invalid/SMOKE-PRIVATE?token=secret"
    )
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
