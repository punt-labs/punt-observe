# Changelog

## [Unreleased]

- Keep active logs and rotated backups within the byte cap under concurrent
  writers, preserving whole records and allowing only individual oversized records.
- Prune and incrementally vacuum idle local telemetry hourly on an interruptible
  daemon, with failure-isolated maintenance and bounded facade shutdown.
- Add private multi-process diagnostics with final-record URL and credential redaction.
- Add schema-validated private OpenTelemetry spans, events, counters and histograms.
- Add private SQLite retention, atomic per-install identifier salts and opt-in OTLP.
