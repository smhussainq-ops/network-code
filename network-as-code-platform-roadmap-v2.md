# Network-As-Code Platform Roadmap v2

Date: 2026-07-02

## Executive Direction

We are building a production-grade network-as-code platform that starts simple for engineers but is rigorous underneath.

The platform should let a network engineer request an outcome, see exactly what the platform will do, review every artifact, prove safety in lab or pre-production, apply only after policy and evidence gates pass, verify live state, and retain an audit trail.

The revised architectural decision is:

> Netcode owns intent, policy, validation, approval, execution control, rollback, evidence, and UI. Rez provides the multi-vendor state/discovery/telemetry driver layer.

This avoids rewriting multi-vendor drivers while keeping production writes under a strict network-as-code control plane.

## Current Baseline

Netcode currently has a working Arista lab slice:

- Simple UI and CLI for an `add_vlan` workflow.
- Intent YAML generation.
- Jinja rendering into Arista EOS config.
- Static validation with fail-closed checks.
- Live outcome panel that shows expected outcome, actual outcome, evidence, and lock state for every click.
- Arista EOS config-session dry-run, apply, verify, and rollback.
- Durable job/change records.
- ORB VM containerlab path for Arista lab testing.
- Basic Rez bridge for state collection when Rez is available.

This is the proof slice. The roadmap below turns it into a multi-vendor production platform.

## What We Reuse From Rez

Rez should be treated as the external multi-vendor state adapter provider.

Reusable components:

- `drivers.collector.DRIVER_MAP`
  - Existing platform registry.
  - Active platforms include `cisco_ios`, `arista_eos`, `cisco_nxos`, `cisco_asa`, `juniper_junos`, `nokia_srl`, `cisco_sdwan`, `palo_alto`, `fortinet`, `aruba_aoscx`, and `meraki`.
- `drivers.base.AsyncBaseDriver`
  - Common async contract: `connect`, `disconnect`, `get_device_info`, `get_interfaces`, `get_routes`, `get_bgp_neighbors`, `get_full_state`.
- `device_state_model.DeviceStateV2`
  - Normalized state model for interfaces, VLANs, routing, BGP, OSPF, LLDP/CDP, ARP, STP, security, counters, hardware, and warnings/errors.
- `drivers.collector.AsyncCollector`
  - Bounded parallel collection pattern.
- `platform_commands.py`
  - Platform aliases and command syntax knowledge.
- `credential_resolver.py`
  - Good pattern for keeping secrets out of scoped inventory and source files.
- Rez math/validation engines
  - Useful later for drift, anomaly, and operational validation, but not as the first enforcement path.

## What We Do Not Reuse Directly

Do not directly reuse these for production change execution without wrapping and hardening:

- Private driver `_send_command` methods as a general write path.
- Generic SSH command execution for arbitrary config.
- LLM/generic parsing as enforcement.
- Huawei VRP driver until schema compliance is fixed.
- Inline credentials from any inventory artifact.

Production writes must go through Netcode-controlled execution adapters with explicit dry-run, apply, verify, rollback, and evidence contracts.

## Target Architecture

### Netcode Control Plane

Responsibilities:

- Intent model.
- Source-of-truth resolution.
- Policy validation.
- Template rendering.
- Change workflow state machine.
- Approval and RBAC.
- Execution adapter contract.
- Rollback contract.
- Evidence and audit.
- UI/API/CLI.
- Job orchestration.

### Rez State Plane

Responsibilities:

- Multi-vendor device discovery.
- Multi-vendor state collection.
- Normalized state snapshots.
- Topology facts.
- Command syntax knowledge.
- Telemetry and operational facts.
- Future anomaly/risk signals.

### Integration Boundary

Netcode calls Rez through a narrow bridge:

```text
collect_state(device) -> normalized state
collect_many(devices, concurrency) -> normalized snapshots
platforms() -> supported platform list
capabilities(platform) -> readable capabilities
```

Later, we can add:

```text
detect_platform(device) -> platform
collect_topology(scope) -> topology graph
validate_live_state(intent, state) -> verdict/evidence
```

Netcode should not call random Rez internals from workflows. Keep the boundary narrow and testable.

## Roadmap Phases

### Phase 0: Preserve The Arista Proof Slice

Status: complete enough for demo and iteration.

Goal:

Keep the current Arista workflow working while the platform architecture expands.

Deliverables:

- Add VLAN workflow.
- Arista dry-run/apply/verify/rollback.
- Live outcome panel.
- Action journal.
- Job records.
- Basic local source-of-truth files.
- ORB lab validation.

Exit criteria:

- Safety blocks bad input.
- Dry-run never commits.
- Apply verifies live state.
- Rollback verifies absence.
- Every UI click shows expected outcome, actual outcome, and evidence.

### Phase 1: Rez Bridge Hardening

Goal:

Make Rez state collection a reliable platform component, not an opportunistic import.

Deliverables:

- Update Netcode Rez bridge to prefer:
  - `NETCODE_REZ_ROOT`
  - `/Users/syedhussain/Dev/Prod/resonance-core`
  - ORB path
  - fallback development paths
- Expose supported Rez platforms in UI.
- Add API endpoint:
  - `/api/adapters/rez/platforms`
  - `/api/adapters/rez/state/{device_id}`
  - `/api/adapters/rez/health`
- Normalize bridge output:
  - `ok`
  - `platform`
  - `driver`
  - `state`
  - `warnings`
  - `errors`
  - `collection_time`
- Add bridge contract tests using fake Rez drivers.
- Add failure handling:
  - Rez missing
  - driver missing
  - auth failure
  - device unreachable
  - schema mismatch

Acceptance criteria:

- UI clearly shows which platforms Rez can read.
- State collection failure never breaks the change workflow.
- State collection evidence is captured in job records.
- Netcode can use Rez state for Arista verification without changing the user workflow.

### Phase 2: Source Of Truth First

Goal:

Make source of truth the input authority before multi-vendor execution expands.

Deliverables:

- Source-of-truth view in UI:
  - sites
  - devices
  - platforms
  - VLANs
  - prefixes
  - policies
  - templates
- Pluggable source-of-truth provider interface:
  - local YAML provider
  - NetBox/Nautobot provider stub
  - ServiceNow/CMDB provider stub
  - IPAM provider stub
- Source-of-truth validation:
  - device exists
  - platform supported
  - site ownership
  - VLAN allocation
  - prefix allocation
  - environment guardrails
- Git-backed intent model:
  - branch per change
  - PR-ready artifact set
  - generated diff
  - report link

Acceptance criteria:

- Platform does not rely on hand-entered device details when source-of-truth data exists.
- Every rendered config can trace back to source-of-truth data.
- UI can explain what data came from source of truth versus user input.

### Phase 3: Unified Workflow State Machine

Goal:

Turn the current procedural flow into a formal workflow engine.

Required states:

- `draft`
- `intent_created`
- `rendered`
- `validated`
- `state_collected`
- `dry_run_passed`
- `approval_required`
- `approved`
- `applying`
- `verified`
- `completed`
- `rollback_available`
- `rolling_back`
- `rolled_back`
- `failed`
- `blocked`

Deliverables:

- Workflow state model.
- State transition rules.
- Evidence required per state.
- Lock/unlock rules per action.
- UI timeline from state machine.
- API that returns allowed next actions.
- Regression tests for illegal transitions.

Acceptance criteria:

- UI buttons are driven by workflow state, not scattered booleans.
- Apply cannot be unlocked by UI bug or stale client state.
- Every blocked state explains missing evidence.

### Phase 4: Multi-Vendor Read And Verify

Goal:

Use Rez to provide read/verify support across multiple vendors before adding write support.

Initial platforms:

- Arista EOS
- Cisco IOS XE / IOS
- Cisco NX-OS
- Juniper Junos
- Fortinet FortiGate
- Palo Alto PAN-OS

Deliverables:

- Multi-vendor state collection page.
- Vendor capability matrix:
  - state collection
  - VLAN visibility
  - interface visibility
  - routing visibility
  - BGP visibility
  - LLDP/CDP visibility
  - security visibility
  - config diff support
  - dry-run support
  - rollback support
- Verification library:
  - `vlan_exists`
  - `vlan_absent`
  - `interface_state`
  - `bgp_neighbor_established`
  - `route_present`
  - `prefix_not_leaking`
  - `management_reachable`
- Post-change verification using Rez state where available.

Acceptance criteria:

- Netcode can verify live state on at least Arista and one Cisco platform using the same verification contract.
- Missing vendor support returns clear unsupported status, not a crash.
- UI shows "can read" separately from "can write."

### Phase 5: Netcode Execution Adapter SDK

Goal:

Create the controlled write path for production-grade multi-vendor changes.

Adapter contract:

```text
render(intent, source_of_truth) -> candidate
precheck(intent, device_state) -> verdict
dry_run(candidate) -> diff/evidence/verdict
apply(candidate) -> execution evidence
verify(intent, post_state) -> verdict
rollback(intent, pre_state) -> rollback evidence
capabilities() -> supported operations
```

Deliverables:

- `ExecutionAdapter` base interface in Netcode.
- Arista EOS implementation refactored into SDK shape.
- Cisco IOS XE lab adapter.
- Cisco NX-OS adapter stub.
- Juniper Junos adapter stub.
- Fortinet/Palo Alto adapter stubs for policy workflows.
- Adapter conformance tests.
- Capability matrix rendered in UI.

Acceptance criteria:

- Every write adapter must implement dry-run/apply/verify/rollback or explicitly mark unsupported operations.
- No adapter can run arbitrary unscoped config.
- Each adapter returns structured evidence.

### Phase 6: Production Safety Controls

Goal:

Make the platform safe enough for limited production pilots.

Deliverables:

- Authentication.
- RBAC:
  - requester
  - reviewer
  - approver
  - operator
  - platform admin
- Approval gates:
  - low-risk lab auto-approval
  - production requires approval
  - high-risk requires second approver
- Secrets abstraction:
  - Vault/AWS Secrets Manager/enterprise secret store integration point
  - no passwords in source files
  - per-environment credentials
- Environment separation:
  - lab
  - pre-production
  - production
- Audit trail:
  - signed report option
  - immutable job records
  - user and approver attribution
- Maintenance windows and change tickets.

Acceptance criteria:

- Production apply cannot happen without identity, authorization, approval, and required evidence.
- Secrets never appear in intent, logs, source-of-truth scope files, or reports.
- Every production change answers: who, what, when, why, proof, rollback.

### Phase 7: Scale Architecture For 25k+ Devices

Goal:

Make the platform architecture scale before doing broad production rollout.

Deliverables:

- Async job queue:
  - Redis/RQ, Celery, or equivalent
  - worker pools
  - retry model
  - timeout model
  - idempotency keys
- Durable database:
  - PostgreSQL for jobs, workflows, approvals, evidence indexes
  - object storage for large artifacts and reports
- Sharding strategy:
  - region
  - site
  - environment
  - vendor
- Concurrency controls:
  - per-device lock
  - per-site limit
  - per-vendor limit
  - change batch windows
- Bulk change model:
  - staged rollout
  - canary devices
  - pause on failure
  - partial success handling
- Observability:
  - job metrics
  - worker metrics
  - adapter metrics
  - API metrics
  - evidence collection latency

Acceptance criteria:

- UI remains responsive while large jobs run.
- A failed device does not cascade blindly across a batch.
- Every large change has canary, pause, retry, rollback, and partial failure semantics.

### Phase 8: Drift, Compliance, And Continuous State

Goal:

Use Rez state collection continuously, not only during a change.

Deliverables:

- Scheduled state snapshots.
- Pre-change and post-change snapshots.
- Intended state versus live state drift engine.
- Source-of-truth versus discovered state drift engine.
- Compliance views:
  - VLAN compliance
  - management-plane compliance
  - routing policy compliance
  - segmentation compliance
  - standard template compliance
- Drift remediation workflow:
  - detect
  - classify
  - approve fix
  - apply
  - verify

Acceptance criteria:

- Platform can prove whether the network still matches intended state after the change window.
- Drift has owner, severity, evidence, and remediation path.

### Phase 9: Broader Infrastructure Automation

Goal:

Expand from network device changes to service-level infrastructure outcomes.

Deliverables:

- Data center fabric workflows:
  - tenant onboarding
  - VRF/VNI lifecycle
  - EVPN/VXLAN validation
  - server port onboarding
- Backbone workflows:
  - BGP peer lifecycle
  - prefix policy updates
  - circuit turn-up
  - PoP rollout
- Cloud networking workflows:
  - VPC/VNet intent
  - transit gateway/cloud router workflows
  - firewall policy lifecycle
  - hybrid route validation
- Terraform integration where cloud APIs are the source of execution.
- Containerlab topology generation for pre-production tests.

Acceptance criteria:

- Platform supports service outcomes, not just isolated device CLI snippets.
- Lab/pre-production validation remains mandatory for risky service-level workflows.

### Phase 10: AI/ML-Assisted Operations

Goal:

Use AI to improve understanding and speed without weakening deterministic controls.

Deliverables:

- Natural-language intake that generates proposed intent.
- Risk summaries:
  - blast radius
  - affected services
  - historical failures
  - current health
  - approval requirements
- Validation explanations:
  - why failed
  - what to fix
  - what evidence is missing
- Change review summaries.
- Rollback runbook generation.
- Predictive signals:
  - interface errors
  - route instability
  - BGP churn
  - capacity trend
  - anomaly detection

Guardrails:

- AI cannot directly apply changes.
- AI output must become structured intent before execution.
- Deterministic validators remain enforcement.
- Human approval remains required where policy demands it.

Acceptance criteria:

- AI helps engineers understand and prepare changes, but cannot bypass policy, approval, validation, or evidence.

## Revised Near-Term Build Plan

### Sprint 1: Rez Integration Hardening

- Fix Netcode Rez root preference to include `/Users/syedhussain/Dev/Prod/resonance-core`.
- Add Rez platforms endpoint.
- Add Rez health endpoint.
- Add UI capability matrix for Rez read support.
- Add fake-driver contract tests.

### Sprint 2: Source-Of-Truth View

- Add UI source-of-truth page/panel.
- Show local YAML inventory, policies, templates, known subnets.
- Mark what came from source of truth versus form input.
- Add source-of-truth provider interface.

### Sprint 3: Workflow State Machine

- Replace UI booleans with backend allowed-action state.
- Persist workflow state transitions.
- Add invalid transition tests.
- Render timeline from workflow records.

### Sprint 4: Rez-Based Verification

- Verify Arista VLAN state using Rez state in addition to direct EOS show commands.
- Add Cisco IOS fake/lab verification for VLAN or interface state.
- Add normalized verification helpers.

### Sprint 5: Adapter SDK

- Define Netcode execution adapter interface.
- Refactor Arista adapter to match interface.
- Add Cisco IOS adapter stub and tests.
- Add vendor capability matrix.

## Success Metrics

- Percent of changes with complete evidence.
- Percent of changes blocked before device contact.
- Percent of changes verified by live state.
- Mean time to validate a change.
- Mean time to rollback.
- Drift detected per site/vendor.
- Reusable workflow count.
- Vendor coverage.
- Device coverage.
- Manual CLI reduction.
- Failed change rate reduction.
- Audit finding reduction.

## Leadership Message

The platform is not "another automation script."

It is a controlled network change operating model:

- Source of truth defines what should exist.
- Intent defines what the engineer wants.
- Policy defines what is allowed.
- Templates define how vendors implement it.
- Rez proves what actually exists.
- Netcode controls if and when anything changes.
- Evidence proves what happened.

That is the foundation for scaling from one Arista lab to multi-vendor production and eventually broader infrastructure transformation.

