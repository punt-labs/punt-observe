# Installed-artifact results

Linux, Python 3.13.18. Package base: `7a9b0545c1f0fec1039c948017b19ecb7ec4aa21`.
No production fixes were made. Evidence root on the execution machine:
`/home/ubuntu/work/telemetry-smoke/` (`package-results/`, `tool-results/`, and
`model-download.json`). The reports preserve full rows and protocol transcripts;
the samples below omit random trace IDs and timestamps.

**Release gate is not clean:** Lux loses buffered usage metrics when its daemon
is stopped before its first periodic export. Details and reproduction below.
All six adopters were installed and exercised, including Quarry with real ONNX
weights. Diagnostic/telemetry files were private; separate product-owned state
directories in biff and Quarry had `0755` modes, noted separately below.

## Package flight

`uv build` produced the wheel and sdist; the wheel was installed in a fresh
external `/tmp/smoke` environment. The installed module reported
`/tmp/smoke/lib/python3.13/site-packages/punt_observe/telemetry/__init__.py`.
OpenTelemetry API/SDK and optional OTLP exporter were 1.45.2.

| Requested check | Result | Key observation |
| --- | --- | --- |
| Build/install | PASS | Both distributions built; installed wheel imported outside checkout |
| Normal CLI | PASS | `meta=1`, `spans=1`, `span_events=1`, `metric_points=2` |
| Privacy/permissions | PASS | Hash/enums/numbers only in user attributes; directories `0700`, db/WAL/SHM/salt/log `0600` |
| Free-text rejection | PASS | Both invalid calls returned normally; zero signal rows |
| Three off switches | PASS | Each: no db, no additional threads before shutdown |
| Eight processes, five rounds | PASS | 40/40 exits 0; every round 8 spans, 8 events, 16 metric points; integrity `ok` |
| Parent plus three forked children | PASS | Child exits `[0,0,0]`; 4 spans, 4 events, 8 metric points; no timeout |
| Idle retention/vacuum | PASS | 128 expired rows removed; freelist 0; page count 282 to 10 without emitting |
| Explicit-consent loopback OTLP | PASS | Traces 322 bytes, metrics 536 bytes, both protobuf-decoded; no local db |
| Ambient OTLP environment | PASS | Fake headers absent, fake endpoint ignored, bodies uncompressed despite ambient gzip |
| Import/open+shutdown timing | PASS | 87.57 ms import; 8.57 ms open+shutdown (single cold-process sample) |

Normal sample values, one row per table:

```text
meta: resource={"punt.schema":1,"service.name":"smoketool"}
spans: smoketool.cli.run v1 span OK
        {"identity":"4542a88524f67ca96726da9c9452803c","outcome":"ok"}
span_events: smoketool.cli.event v1 {"result":"ok"}
metric_points: smoketool.cli.count v1 counter unit=1 value=2.0 attrs={}
```

The other metric row is the histogram: count 1, sum 12.5, unit `ms`, empty user
attributes. SQLite reported `integrity_check=ok`, `journal_mode=wal`,
`user_version=1`, `auto_vacuum=2` (incremental). The sole persisted `meta` entry
is the resource: **there is no persisted retention-days metadata**. Retention
was configured to 90 days normally and 1 day for the idle probe. The latter used
the internal 30 ms `providers._PRUNE_INTERVAL_S` knob because public config has
no interval field. This is a behavioral retention proof, not metadata inference.

Invalid calls produced only this rate-limited diagnostic, without rejected data:

```text
Telemetry dropped: unknown_attribute
```

The positive logging probe became
`https://example.invalid/…(segments=1)`. For the adopted-tool probes it became
`https://example.invalid/…(segments=5)`, with the distinctive marker removed.
No OTLP logs endpoint was used because the package exports traces/metrics only.

## Adopted main snapshots and pins

All six `uv.lock` files and actual installed `direct_url.json` provenance point
to `53e657555cab240795c850ea7f0e4dd77a8d98fc`, an explicitly allowed pin. No
unexpected pins were found. Five declare the Git revision directly in project
dependencies; biff declares a version range with that Git revision in
`[tool.uv.sources]`. Installs were non-editable, in separate snapshot venvs;
original working branches were not switched or edited.

| Repo | Fetched main snapshot | Installed observe pin |
| --- | --- | --- |
| punt-kit | `d7916b76eb4bdeb36ccd8a4daa9dbe485050a8df` | `53e6575` |
| z-spec | `7c7c15a30d59a54d3c76d82a9533b345c1121375` | `53e6575` |
| lux | `1fae271be581f18a063ebe2cf0ce89f75e451125` | `53e6575` |
| biff | `200bf8a6aae911c17623e7f90e2ad70f50dd9225` | `53e6575` |
| quarry | `654ba8cffea4e3b7288337e29e7c809c136b1031` | `53e6575` |
| vox | `23cc6908acf6d49d2128e6a17203f7ecbb916d41` | `53e6575` |

Counts below use `(spans, span_events, metric_points)`; every existing db also
had one `meta` resource row. Standalone events occupy `spans`, not
`span_events`, by design. Every inspected database returned integrity `ok`.

| Tool | Result | CLI and six-process evidence | MCP evidence |
| --- | --- | --- | --- |
| punt-kit | PASS | `punt version`: `(1,0,0)`; concurrent `(6,0,0)` | No MCP server in this tool |
| z-spec | PASS | `z-spec disable`: `(1,0,0)`; concurrent `(6,0,0)` | `check(file=probe)`: typed failed/spec-not-found event; `(2,0,0)` includes setup enable event |
| lux | FAIL short-stop flush; PASS periodic export/privacy | `lux scene show`: baseline plus six concurrent renders exit 0; quick-stop db has `(0,0,0)`; periodic control stores 1 histogram containing all 8 renders including HTTP MCP |
| biff | PASS telemetry; state-mode note below | `biff version`: `(1,0,0)`; concurrent `(6,0,0)` | `finger(user=probe)`: ok typed span; `(2,0,0)` includes setup enable event |
| quarry | PASS telemetry; state-mode note below | Real `quarryd` + `quarry find`: one baseline plus six concurrent searches, all exit 0; with MCP totals `(8,0,0)` | Real stdio `find(query=probe)` reaches daemon; typed search span, surface=mcp |
| vox | PASS | `vox version`: `(1,0,0)`; concurrent `(6,0,0)` | `speak(mode=probe)`: typed error span; `(2,0,0)` includes CLI mcp span |

Both switches left no db and unchanged stdout/stderr/exit for every tool.
For Lux and Quarry, this was also checked with a **fresh daemon** started under
each switch and the same real rendering/search command. Client-only switches
cannot change an already-running daemon's environment. `punt-kit`'s canonical
tool name is `punt`; therefore its per-tool switch and global switch are both
`PUNT_TELEMETRY`, not `PUNT_KIT_TELEMETRY`.

All six concurrent CLI invocations for every tool exited `[0,0,0,0,0,0]`; no
SIGBUS or hangs occurred. `lux version` and `quarry version` were initial
non-emitting controls, not the telemetry proof: daemon-backed render/search
operations above supplied the real emitting/concurrent checks.

### Typed samples and MCP privacy

```text
punt.cli.command: {"command":"version","json":false,"outcome":"ok"}
z-spec.cli.disable: {"duration_ms":0.4662,"outcome":"ok"}
z-spec.mcp.check: {"duration_ms":0.1223,"failure":"spec_not_found","outcome":"failed"}
biff.cli.invoke: {"command":"version"}
biff.mcp.call: {"outcome":"ok","tool":"finger"}
vox.cli.command: {"command":"version","outcome":"ok"}
vox.mcp.tool: {"outcome":"error","tool":"speak"}
lux.hub.operation_duration: histogram count=8, attrs={"operation":"render","outcome":"ok"}
quarry.daemon.search: {"database_hash":"922d50421132204e2b2b5e2d893619ac",
 "empty_query_hash":"49d6af4baef1f53611619828ee4979e8","query_length":43,
 "latency_ms":27.1047,"memory_query":false,"result_count":0,"surface":"mcp"}
```

All stdio MCP servers received initialize, tools/list and tools/call. z-spec
and biff were enabled in throwaway repositories, and biff received its required
valid `_caller` identity. Without these, their normal gates refuse the call
before usage middleware; those initial refusal paths were not counted as proof.
The z-spec nonexistent-file and vox invalid-mode probes deliberately exercise
typed error outcomes without external fuzz/probcli, audio or paid APIs.

Lux has no shipped stdio command anymore. The stdio probe adapts its actual MCP
registry with actual `DaemonRuntime`, not fakes; it stored one typed render
histogram. Separately, the shipped daemon's **actual HTTP MCP** initialize,
tools/list and scene_show call returned HTTP `200,202,200,200`, and the call is
included in the real daemon's eight-render histogram. The additional 202 is the
initialized notification. No display window was needed or visually verified.

The distinctive marker and `/home/ubuntu/private/path` both had **zero matches**
under `grep -a -l -F -e ...` in db files, available sidecars and logs. Each grep
returned 1 (no match), not an error. Full fake values naturally appear in protocol
requests/responses kept in the external smoke evidence, not in telemetry/logs.
MCP telemetry contained the declared names, closed enums, bounded numbers and
opaque hashes shown above, never the raw argument.

### Permissions and external dependencies

All observed diagnostic/telemetry boundaries (`~/.punt-labs`, tool directory,
logs directory) were `0700`; db, observed WAL/SHM, salt when used, log and rotation
lock files were `0600`. Clean daemon shutdown may remove SQLite sidecars.

Separate application state is not uniformly `0700`: biff created `unread`,
`sentinels`, `active` and a sentinel subdirectory as `0755`; Quarry created
`data/default/lancedb` ancestors as `0755`. Their enclosing tool directories
were `0700`, so this is not evidence of access from another user, but it does
not meet a literal all-subdirectories-must-be-0700 check. No mode changes made.

Quarry initially could not boot without model files. The public 106 MiB INT8
Snowflake ONNX model and tokenizer were then successfully downloaded at revision
`e58a8f756156a1293d763f17e3aae643474e9b8a`; the real CPU backend, daemon, CLI
and MCP searches all ran. No fake embedder was used. Display GUI, audio synthesis,
paid providers and actual Z type/model-checking were intentionally not run.

Some original repos' direnv files were blocked during snapshot preparation.
They contain developer secrets and telemetry-disabling settings, so they were
not approved or sourced. Smoke subprocesses deliberately use explicit isolated
environments and their own installed dependencies; no secret loading was needed.

## Lux short-stop data loss: reproducible failure, not fixed

```sh
python smoke/run_daemons.py /path/to/snapshots lux
```

Observed: one render plus six concurrent renders all exited 0, daemon stopped
within the timeout with exit `-15` (SIGTERM), but the db contained **0 metric
points**. The preserved failing HOME is
`tool-results/lux-daemon-69e63m6l/` under the evidence root; integrity remains ok.

Discriminating control:

```sh
python smoke/run_daemons.py /path/to/snapshots lux --wait-for-export
```

Waiting for the real 60-second metric interval yielded one histogram row with
count **8** (baseline, six concurrent CLI renders, HTTP MCP render), proving the
real rendering paths record usage. The adapted stdio lifecycle also flushed its
single render at EOF. Thus “render operations never emit telemetry” is disproved.

A likely shutdown integration boundary is Lux's `DaemonRuntime` context outside
uvicorn's `server.run()`: the installed uvicorn `capture_signals()` re-raises the
original SIGTERM before returning to that outer context. The exact shutdown
call sequence was not traced, so this is a code-backed explanation, **not a
confirmed root cause**. The observed short-stop metric loss itself is reproduced.

## Local package quality gate

```text
All checks passed!
All checks passed!
53 files already formatted
Success: no issues found in 38 source files
0 errors, 0 warnings, 0 informations
======================= 675 passed, 4 warnings in 13.30s =======================
```

The four warnings are the existing deliberate multithreaded-fork deprecations.
This quality gate is separate from the installed-artifact proof above. No macOS
execution or GUI verification was performed in this flight.
