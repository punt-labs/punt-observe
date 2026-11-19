# punt-observe

Shared diagnostics and telemetry for Punt Labs Python applications. Python 3.13+,
POSIX only (Linux and macOS). Follow punt-kit standards.

## Quality gate

Run `make check` (lint, strict mypy + pyright, tests). `make build` also validates
wheel and sdist. Use `uv`, the committed lockfile and `src/` layout.

## Architecture

`logging` owns entry-point dictConfig, final-record redaction, private regular
files and flock-serialized multi-process rotation. Libraries never configure logs.
`telemetry` validates versioned schemas before private OTel providers. Exporters
are configuration, never imported by tool instrumentation. Resources never use
environment/host detectors. Local SQLite is private and retention-bounded;
network export requires explicit config consent. Disabled means no store/thread.

See DESIGN.md for binding choices. Never log content or rejected telemetry values.
Public API docstrings follow PEP 257 and Google style; module docstrings everywhere.

## Tracking

Linear team **PL** is the tracker of record. Do not initialize a separate tracker.
