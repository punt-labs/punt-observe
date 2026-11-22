# punt-observe

Shared private diagnostics and privacy-safe local telemetry for Punt Labs Python tools.
Python 3.13+, Linux and macOS. Licensed under MIT.

## Install

```sh
uv add punt-observe
# Optional network exporter:
uv add 'punt-observe[otlp]'
```

## Diagnostics

Each CLI, MCP, hook, daemon and detached worker configures its own role. Libraries
only create module loggers. No output is written to stdout.
The package ships no executable and does not configure logging or collect
usage for itself. Internal telemetry failure warnings use the consumer's
diagnostic sink, never the telemetry pipeline. The development smoke programs
stand in for consuming tools and run against isolated home directories.

```python
import logging
from punt_observe.logging import configure_logging

configure_logging("my-tool", "cli")
logger = logging.getLogger(__name__)
logger.info("Completed operation: items=%d", 3)
```

Files live at `~/.punt-labs/my-tool/logs/my-tool.log`, UTF-8, INFO by default,
5 MiB plus five backups. Directories are 0700; files and backups are 0600.
Rotation is safe across cooperating processes using a stable flock lock.
Setup permission failures raise `LoggingSetupError`; append failures are best-effort.
Successful configuration writes an INFO startup record identifying the role and
process ID, regardless of the file's log level or whether a terminal is attached.
Final formatted records are redacted, control-escaped and bounded to 64 KiB including
their newline. Redaction is defense in depth: **never log content or secrets**.
Home-directory prefixes become `~` on path-component boundaries, including in
tracebacks and direct `AtomicAppendLog.append()` calls. Candidates include the
current `HOME` (read at redaction time), the current uid's passwd home and their
resolved aliases. Alias resolution is cached by input; empty, relative and root
homes are ignored. This does not mask standalone account names or other users'
homes, and cached aliases do not track symlink retargeting.
Direct appends also apply the redactor's 64 KiB text cap before single-line escaping.

## Telemetry

Declare schemas once; instrument through the facade, never an exporter.

```python
from punt_observe.telemetry import (
    EnumAttr,
    HashAttr,
    SignalSchema,
    TelemetryConfig,
    open_telemetry,
)

schemas = [
    SignalSchema(
        "my-tool.cli.run",
        1,
        "span",
        {
            "outcome": EnumAttr(("ok", "error")),
            "item": HashAttr(),
        },
    )
]
telemetry = open_telemetry("my-tool", schemas, TelemetryConfig())
with telemetry.span("my-tool.cli.run", 1, item=telemetry.hash_id("local-id")):
    pass  # Perform the operation. Exceptions propagate without recording text.
telemetry.shutdown(timeout_s=2)
```

Use `event(name, version, **attrs)`, `count(name, version, n=1, **attrs)` or
`record(name, version, value, **attrs)` with matching event/counter/histogram schemas.
Attribute types: closed EnumAttr, BoolAttr, IntAttr, DurationAttr (ms), CountAttr,
SizeAttr (By), LengthAttr and HashAttr. Unknown attributes/versions, wrong types,
free text and arbitrary strings posing as hashes are dropped before OTel.
Only explicit schema-derived lengths and salted hashes may derive from content.

Local SQLite capture defaults on at `~/.punt-labs/my-tool/telemetry.db`, separate
from diagnostics. Store and SQLite sidecars are private, retained 90 days by
default (configurable 1–3650); startup and hourly export-triggered pruning reclaim
space incrementally. Per-install salt is `~/.punt-labs/telemetry.salt` (0600).

**Any off switch wins:** `PUNT_TELEMETRY=off`, `MY_TOOL_TELEMETRY=off`, or
`TelemetryConfig(enabled=False)`. Also accepts 0/false/no, case-insensitively.
Disabled telemetry creates no store, salt, provider or worker.

Network export requires `TelemetryConfig(exporter="otlp", otlp_consent=True,
otlp_endpoint="https://collector.example")`. Environment variables
`PUNT_TELEMETRY_EXPORTER` and `PUNT_TELEMETRY_OTLP_ENDPOINT` may select the
exporter/endpoint but **cannot grant consent**. Missing optional OTLP dependency
falls back to SQLite. Instrumentation stays identical.
Collector failure diagnostics exclude response text and request exception details;
the library reports dropped export batches through rate-limited diagnostic warnings.

Providers are private, never process-global. Pending span export is bounded at
2048; saturation drops records, not tool results. Shutdown bounds waiting; a
misbehaving exporter may finish on a daemon thread after the deadline.
`shutdown(timeout_s=2)` force-flushes pending metrics, spans and events without
waiting for their periodic export interval. As a fallback, normal Python exit
(including `sys.exit` or an uncaught `KeyboardInterrupt`) flushes pending live
providers through one atexit registry, with a **two-second total** wait across
all instances. Explicitly shut-down instances and forked children that have not
emitted do no exit work. Explicit shutdown remains the preferred lifecycle.

**The library never installs signal handlers.** A SIGTERM with its default
disposition skips atexit, as do SIGKILL and `os._exit`. Daemons must convert
SIGTERM into normal Python unwinding (for example, raise `SystemExit`), or call
`shutdown` before terminating. A server that restores and re-raises default
SIGTERM before an outer telemetry context unwinds still skips that context and
atexit; move explicit shutdown into its graceful lifecycle instead.

## Development

`uv sync --frozen`; `make check`; `make build`. CI checks Linux and macOS.
`make help` lists targets. The original append protocol is from punt-labs/vox;
redaction and URL grammar are from punt-labs/quarry (MIT, Punt Labs).
