# Design decisions

Binding input: `punt-observe-design.md`, implementing punt-kit LOG-1–16,
TEL-1–10 and PY-LOG-1–9. Python 3.13+, MIT, Linux and macOS.

## ADR-001: Private providers, never globals

Use private SDK TracerProvider and MeterProvider instances with explicit
Resource attributes. Never set application-global providers, call Resource.create,
or run host/environment detectors. This keeps host identities and arbitrary
OTEL_RESOURCE_ATTRIBUTES outside payloads and avoids changing embedding applications.

## ADR-002: Validate before OTel

Versioned allowlists, closed enums, numeric ranges and typed derived hashes gate
all calls before OTel. No free text, exception recording, status descriptions or
unvalidated resource attributes. Span outcomes are schema-declared ok/error enums.
Rejected events are dropped and reported with fixed, rate-limited reason codes.

## ADR-003: Atomically link the install salt

Write 32 random bytes into a private same-directory temporary file, fsync it,
then link to telemetry.salt without replacement. A racing loser reads the winner.
Use HMAC-SHA256 truncated to 128 bits; never store raw IDs or salt in telemetry.

## ADR-004: SQLite incremental vacuum

Precreate private files before connecting. Use WAL and busy_timeout=2000,
auto_vacuum=INCREMENTAL, user_version=1, startup/hourly export-triggered pruning,
and independent hourly maintenance so idle daemons also enforce retention. The
provider bundle owns an interruptible daemon only when it owns a local store;
the worker interval is injectable for tests. Each maintenance pass uses the same
prune and incremental-vacuum operation, reclaiming storage rather than just rows.
Failures emit only the existing rate-limited metadata diagnostic and do not end
the schedule. Disabled capture and network-only export create no pruning worker.
Retain 90 days by default; supported range is 1–3650 days, never unlimited.

## ADR-005: POSIX-only append protocol

Port vox's O_APPEND plus stable flock rotation lock and reopen every append.
Windows is not supported: no unsafe lock-free fallback. Port quarry's AuthorityMask,
LogForm and final-record redaction semantics, with a byte cap applied after escaping.
The formatter indents traceback continuations; durable sink escapes those to one
physical line. Creation uses 0600, directory 0700, O_NOFOLLOW and fd tightening.

## ADR-006: Bounded shutdown and optional network export

BatchSpanProcessor bounds pending spans at 2048. Private metric readers bound
export time; facade shutdown bounds its wait even for a misbehaving exporter.
A timed-out exporter may finish on its daemon worker; it never stalls the tool.
Shutdown cancels scheduled pruning immediately. In-flight maintenance is joined
by the bounded facade's shutdown worker before closing the shared store; a stuck
maintenance pass may finish later on its daemon, without extending caller waits.
OTLP is lazily imported and requires config consent; missing extra falls back local.
Exporter injection is an optional config factory for deterministic no-network tests.
