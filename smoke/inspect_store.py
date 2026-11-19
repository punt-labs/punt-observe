"""Inspect telemetry read-only in intent, including live SQLite sidecar modes."""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path

SECRETS = (b"SMOKE-SECRET-4242", b"/home/ubuntu/private/path", b"SMOKE-PRIVATE")


def inspect(home: Path, tool: str) -> dict[str, object]:
    root = home / ".punt-labs"
    db = root / tool / "telemetry.db"
    result: dict[str, object] = {"db_exists": db.exists()}
    if db.exists():
        with closing(sqlite3.connect(db)) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            result["counts"] = {
                t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                for t in tables
            }
            result["samples"] = {
                t: conn.execute(f'SELECT * FROM "{t}" LIMIT 1').fetchone()
                for t in tables
            }
            result["signals"] = [
                list(r)
                for r in conn.execute(
                    "SELECT name, attributes FROM spans ORDER BY name LIMIT 20"
                )
            ]
            result["metrics"] = [
                list(r)
                for r in conn.execute(
                    "SELECT name, count, attributes FROM metric_points"
                )
            ]
            result["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            result["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
            result["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
            result["auto_vacuum"] = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
            result["metadata"] = dict(conn.execute("SELECT * FROM meta"))
            result["modes"] = {
                str(p.relative_to(home)): oct(stat.S_IMODE(p.stat().st_mode))
                for p in [root, *root.rglob("*")]
                if p.is_file() or p.is_dir()
            }
            result["secret_hits"] = [
                str(p.relative_to(home))
                for p in root.rglob("*")
                if p.is_file() and any(secret in p.read_bytes() for secret in SECRETS)
            ]
    result["modes"] = {
        **result.get("modes", {}),
        **{
            str(p.relative_to(home)): oct(stat.S_IMODE(p.stat().st_mode))
            for p in [root, *root.rglob("*")]
            if p.exists() and (p.is_file() or p.is_dir())
        },
    }
    logs = list(root.glob(f"{tool}/logs/*.log"))
    result["logs"] = {p.name: p.read_text(errors="replace")[-4000:] for p in logs}
    result["log_secret_hits"] = [
        p.name for p in logs if any(secret in p.read_bytes() for secret in SECRETS)
    ]
    return result


if __name__ == "__main__":
    sys.stdout.write(json.dumps(inspect(Path.home(), sys.argv[1]), indent=2) + "\n")
