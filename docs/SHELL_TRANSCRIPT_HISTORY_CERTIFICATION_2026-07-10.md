# Shell Transcript History Certification - 2026-07-10

## Verdict

PASS. Netcode Shell sessions now have a durable, tenant-scoped session index and
complete JSONL transcript. A closed session remains searchable and readable after
the Netcode control plane restarts.

## User Path

- Open `Shell` in the Rez UI.
- While a session is active, use `View session log` in the Session evidence card.
- At any time, use `Session History` in the Shell header.
- Select `View transcript` to reopen a saved session.
- Use `Download JSON` for a portable audit artifact.

## Recorded Data

- Session open and close timestamps.
- Organization, device, platform, connector, and connector pool.
- Direct or guarded mode.
- Commands and command type.
- Complete terminal output frames.
- Optional Netcode change attachment.
- Whether device configuration was touched.
- Final session status, command count, and output byte count.

Device credentials are not part of the transcript contract.

## Persistence Design

- Session metadata: `shell_sessions` in the Netcode durable store.
- Transcript artifact: `reports/shell-<session-id>.jsonl`.
- Live PTY/WebSocket state remains process-local and is intentionally not resumed
  after a process restart.
- Legacy JSONL transcripts are indexed once per workspace and shown as archived.
- History is tenant-scoped and cursor-paginated in pages of at most 50 in the UI.

## Automated Validation

```text
pytest tests -q
157 passed

pytest tests/contracts/test_netcode_shell_bridge.py \
       tests/contracts/test_rbac_middleware.py -q
65 passed

vitest netcodeShellProtocol.test.ts netcodeDeviceCatalog.test.ts
8 passed

eslint NetcodeShellPage.tsx
pass
```

Contract coverage proves:

- Transcript access after clearing all in-memory Shell sessions.
- Command and terminal-output persistence.
- Legacy transcript indexing.
- Organization isolation.
- Searchable device catalog and Shell bridge behavior remain green.

## Live Proof

Environment:

- Rez UI: `http://127.0.0.1:4005`
- Rez backend: `http://127.0.0.1:9005`
- Netcode control plane: `http://127.0.0.1:8095`
- Local Connector: ORB `clab`, 26 synchronized devices.

Session:

```text
session_id: b6861997da14453e
device: v2-hq-core
command: show version | include Uptime
result: Uptime: 23 hours and 40 minutes
status: closed
commands: 1
terminal output: 161 bytes
device config: not touched
```

Acceptance sequence:

1. Opened `v2-hq-core` from the Rez Shell device catalog.
2. Ran the command through the real xterm/WebSocket/local-connector PTY.
3. Confirmed the command and output appeared in `View session log`.
4. Closed the Shell session.
5. Confirmed the record appeared in `Session History` as `closed`.
6. Restarted the Netcode control plane on port 8095.
7. Confirmed the Local Connector reconnected.
8. Reopened session `b6861997da14453e` from Session History.
9. Confirmed the original command, uptime output, and close record remained present.

## Scope

This certifies durable Shell history and transcript retrieval. It does not claim
live PTY resumption across a backend restart; a new live session must be opened.
