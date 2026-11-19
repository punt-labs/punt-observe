# Installed telemetry smoke flight

These are standalone subprocess/protocol probes, not pytest tests. No production
code is changed. Evidence includes temporary homes, databases, logs, JSON reports
and OTLP protobuf bodies, kept outside the checkout.

## Package

Run `make smoke`. It builds a wheel, installs it and the optional OTLP exporter
into `/tmp/smoke`, then runs the installed interpreter with clean child
environments and a fresh HOME for every case. Override `SMOKE_VENV` and
`SMOKE_OUTPUT` to change these locations. Start with a nonexistent venv for a
fully fresh installation; an existing smoke venv is deliberately preserved.

The CLI declares a span, event, counter and histogram, then tests normal emission,
invalid/free-text rejection, all three off switches, real forked children, and
idle expiry/vacuum. Five concurrency rounds each launch eight **subprocesses**;
the Python thread pool only supervises those independent CLI processes.

The idle probe uses the internal `providers._PRUNE_INTERVAL_S` knob (30 ms), not a
public interval setting. OTLP uses a loopback HTTP receiver, decodes actual trace
and metric protobufs, and records ambient fake-header/endpoint/compression
isolation. This package does not export OTLP logs.

`package.json` records complete rows, live file modes, SQLite PRAGMAs, logs,
subprocess exits and timing. SQLite's `meta` table currently stores the resource
only: retention days are config, not persisted metadata. Retention is checked
behaviorally. Inspector-opened sidecars are distinguished from the CLI's
`live_modes` collected before shutdown.

## Adopted tools

Create snapshots of fetched `origin/main` under an external directory, one
subdirectory per repository: `punt-kit`, `z-spec`, `lux`, `biff`, `quarry`, `vox`.
Do not switch or modify the repositories' working branches. Install each snapshot
into its own `<snapshot>/.venv` with Python 3.13. Respect the `punt-observe` Git
source in `pyproject.toml`/`uv.lock`, including biff's `[tool.uv.sources]` override
(ordinary pip does not honor that section).

Using any Python 3.13 interpreter:

```sh
python smoke/provenance.py /path/to/snapshots /path/to/original/repos
python smoke/run_tools.py /path/to/snapshots
python smoke/run_daemons.py /path/to/snapshots
python smoke/run_daemons.py /path/to/snapshots lux --wait-for-export
```

The tool runner records CLI output, both off switches, six concurrent CLI
processes, private-store inspection, a positive redaction probe through each
tool's installed logging wrapper, and stdio JSON-RPC initialize/list/call. It
enables z-spec and biff only in throwaway Git repositories; biff's call supplies
the required harness `_caller` and disables the remote relay. The distinctive
argument is `SMOKE-SECRET-4242 /home/ubuntu/private/path`. `grep -a -l -F` checks
the marker in the database, sidecars and logs (exit 1 means no match).

Lux no longer ships a stdio launch command. `lux_stdio.py` adapts its real MCP
registry using its real `DaemonRuntime`, without mocks. The separate daemon
flight boots the shipped `luxd-hub`, runs `lux scene show`, six concurrent renders,
and also speaks JSON-RPC to the actual HTTP MCP endpoint. Lux's histogram exports
on a 60-second interval; `--wait-for-export` waits for an actual stored point
before stopping, rather than assuming immediate export.

Quarry requires its ONNX model before the daemon can serve. The environment
sets Hugging Face offline mode to prevent unexpected downloads, using a shared
`<snapshots>/model-cache` for model assets only (telemetry HOME remains fresh).
Preload the public model before running the daemon flight:

```sh
HF_HOME=/path/to/snapshots/model-cache HF_HUB_DISABLE_TELEMETRY=1 \
  /path/to/snapshots/quarry/.venv/bin/python -c \
  'from quarry.embeddings import OnnxEmbeddingBackend; print(OnnxEmbeddingBackend.download_model_files())'
```

Without cached weights, `quarry version` and the stdio protocol/error path still
run, but they do not exercise daemon recall telemetry. Treat that as a **skip**,
not telemetry success. The recorded flight downloaded the real 106 MiB INT8
model and completed the daemon-backed checks. No GUI display, audio synthesis,
paid API or collector credentials are used.

`tool-results/*.json` contain full transcripts and observations; inspect these
rather than treating a zero CLI exit as proof of telemetry. `punt-kit` uses the
tool name/directory `punt`, so its per-tool off variable is the same
`PUNT_TELEMETRY` as the global switch. Product-owned state directories outside
the diagnostic sink may have different modes; these are reported, not repaired.

## Recorded release-gate findings

See `RESULTS.md` for the executed snapshot matrix, sample values and limitations.
These probes intentionally do not fix any package or adopter bugs.
