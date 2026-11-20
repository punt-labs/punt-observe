# Changelog

## [Unreleased]

- Add a tag-triggered, smoke-gated TestPyPI and PyPI trusted publishing workflow.
- Defer shutdown's registry and pruning locks to its bounded worker and make
  diagnostic rate limiting reentrant, avoiding signal-handler shutdown deadlocks.
- Publish exit work before starting its worker and reject activation after exit
  begins, preventing missed flushes and stranded exit workers during registration.
- Force-flush metrics and queued spans/events at shutdown; flush pending providers
  on normal process exit through one failure-isolated, two-second-total atexit
  registry. Explicit shutdown cancels exit work; forked children opt in only after
  emitting. Default SIGTERM still requires a daemon-owned graceful lifecycle.
- Share one weak, live-instance fork registry for punt-observe and remove instances
  at shutdown; OpenTelemetry's own per-instance fork callbacks remain unchanged.
- Preserve SQLite shared-memory locks during concurrent telemetry export by
  securing sidecars only before connecting.
- Keep active logs and rotated backups within the byte cap under concurrent
  writers, preserving whole records and allowing only individual oversized records.
- Prune and incrementally vacuum idle local telemetry hourly on an interruptible
  daemon, with failure-isolated maintenance and bounded facade shutdown.
- Add private multi-process diagnostics with final-record URL and credential redaction.
- Add schema-validated private OpenTelemetry spans, events, counters and histograms.
- Add private SQLite retention, atomic per-install identifier salts and opt-in OTLP.
