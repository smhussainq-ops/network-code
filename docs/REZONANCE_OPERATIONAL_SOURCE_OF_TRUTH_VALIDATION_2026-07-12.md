# Rezonance Operational Source of Truth Validation

**Date:** 2026-07-12

**Verdict:** PASS for the native pilot foundation

**Scope:** Netcode control plane, Rezonance Network Model, Local Connector observations, Rez Diagnostics, Digital Twin, Shell, Git-backed lifecycle, and rollback consistency.

## What was proven

- Netcode owns one tenant- and environment-scoped operational model.
- Approved intent, fresh observations, and immutable operational history remain separate.
- Discovery and telemetry produce proposals/observations; they cannot silently become approved intent.
- Rez consumes the active model read-only and can create only typed remediation drafts.
- Normal plans and Rez drafts produce deterministic candidate model revisions.
- Dry-run does not write; human approval is required before apply.
- A candidate becomes active only after live verification.
- Device rollback restores the linked parent model through an isolated Git checkpoint.
- A superseded change cannot roll the model back after a newer revision is active.
- Digital Twin, Network Model UI, planner, Rez, and Shell resolve the same canonical device identity.
- Initial model/catalog reads open zero device connections.

## Marcus live journey

### Investigation

Question submitted through Chat V2:

> Is there an issue between v2-campus-core and the rest of the network? Users are complaining of connectivity; do a thorough live check and find the root cause.

Rez incident: `20260712_163146_chat_bf8a71a5`

Confirmed result:

- root: `L1_INTERFACE_ADMIN_DOWN`
- confidence: `0.98`
- target: `v2-campus-core Ethernet1`
- peer symptom: `v2-campus-edge-1 Ethernet1` operationally down
- supporting deltas: missing LLDP/OSPF adjacency on the failed link
- service classification: `DEGRADED but operational`, because the approved redundant Ethernet2 path, routes, ping, and BGP remained healthy
- scope: 21 commands, 3 devices, 21 findings
- unrelated state was not promoted as the root

### Rez-to-Netcode remediation

Original reviewed change:

- Rezonance ID: `REZ-CHG-20260712-E0A3840276A1`
- canonical change: `e0a38402-76a1-44ca-b32d-af009a22d853`
- candidate model: `change-e0a38402-76a1-44ca-b32d-af009a22d853`
- parent model: `arista-enterprise-v2-2026-07-12-hq-store-path`

Exact apply:

```text
interface Ethernet1
   no shutdown
```

Exact rollback:

```text
interface Ethernet1
   shutdown
```

The rollback is an exact administrative-state inverse. It does not use `default interface` and cannot erase unrelated configuration.

### Governed execution

1. Static policy and deterministic rendering passed.
2. EOS created a candidate configuration session.
3. The dry-run staged the two commands, captured the diff, and aborted the session without a write.
4. `marcus-approver` approved the candidate separately from requester `rez-rca`.
5. The Local Connector applied and committed the exact commands.
6. Live verification read Ethernet1 as `up/up`.
7. Only after verification did the candidate model become active.
8. Git recorded approval `6b6dbce` and verification `ad55ef7` in the isolated change-history repository.

### Adversarial rollback and recovery

The verified change was rolled back from the UI:

- device proof: Ethernet1 became administratively down
- workflow state: `rolled_back`
- model event: `network_model_rollback` with `ok: true`
- active model restored to `arista-enterprise-v2-2026-07-12-hq-store-path`
- Git rollback checkpoint: `4a077fb`

The stale original candidate was not reused. A new reviewed change restored service:

- Rezonance ID: `REZ-CHG-20260712-7115482645D4`
- canonical change: `71154826-45d4-4781-9d5b-ccd9c1494187`
- candidate model: `change-71154826-45d4-4781-9d5b-ccd9c1494187`
- approver: `marcus-approver-2`
- Git approval: `9112576`
- Git verification: `0d3ed76`
- final live state: `Ethernet1 is up, line protocol is up (connected)`
- final model state: new candidate `active`

This proves rollback restores both device and model state, and subsequent recovery must use a fresh reviewed delta.

## Shell proof

Marcus opened `v2-campus-core` through its assigned Local Connector and ran:

```text
show interfaces Ethernet1 | include line protocol
```

The live result was `Ethernet1 is up, line protocol is up (connected)`.

The closed session remained available in Session History:

- session: `1e22f80f0ca24d7a`
- device: `v2-campus-core`
- commands: 1
- output bytes: 206
- transcript: `reports/shell-1e22f80f0ca24d7a.jsonl`

The transcript includes session-open, command, output, and session-close records.

## Contract and scale validation

| Gate | Result |
|---|---|
| Netcode full suite after rollback hardening | 289 passed |
| Netcode model/lifecycle targeted suite | 47 passed |
| Rez targeted contracts | 85 passed |
| Rez UI behavior suite | 175 passed |
| Rez Vite production bundle | passed, 2,861 modules |
| 10,000-device paginated query | under 2 seconds, bounded page |
| Diff/whitespace check | passed |
| Python compile checks | passed |

Security and integrity contracts cover tenant isolation, missing/ambiguous identity, replay/idempotency, no secrets in model payloads, stale observations, failed scans, partial coverage, human approval, failed verification, fleet activation, rollback linkage, and stale rollback rejection.

## Honest residual debt

The source-of-truth slice does not make every pre-existing repository test green:

- Rez repository-wide Python run: 4,536 passed, 124 failed, 19 skipped, 31 xfailed, and 3 collection errors.
- Remaining failures are established global/environment baselines: anonymization intentionally disabled for this lab, live-lab-dependent suites, async fixture/plugin setup, and pre-existing math regressions outside this slice.
- Repository-wide TypeScript and lint commands remain red from pre-existing errors outside the changed UI files. Focused UI behavior tests and the Vite production bundle pass.

These are not hidden or counted as source-of-truth passes. They should remain separate stabilization work before a broad production release.

## Pilot boundary

This foundation is standalone. NetBox, Nautobot, Infoblox, FortiManager, Panorama, and CMDB integrations are optional future authority/observation connectors. They are not prerequisites and must not bypass model review, Local Connector credential custody, human approval, verification, or rollback.
