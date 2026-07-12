# Netcode Marcus P0-P2 execution plan - 2026-07-12

## Product boundary

Netcode owns the operational model needed for automation and Rez RCA: discovered
devices, interfaces, sites, roles, links, routing, software versions, approved
standards, change records, and the Digital Twin.

Netcode will not pursue Nautobot/NetBox DCIM or IPAM integration. It will not try
to model racks, power, physical asset contracts, or become a general IP address
authority. A later Infoblox integration is the explicit IPAM/DNS/DHCP path:
read and reconcile first, then add human-approved reservations only after pilot
validation.

## User language

Backend state names remain stable. The primary UI uses:

| Internal term | Marcus-facing language |
|---|---|
| runner | Local Connector |
| canary | First-device test |
| batch | Rollout group |
| intent | Desired change |
| apply | Push change |
| verify | Confirm result |
| artifact | Change record |
| RCA handoff | Investigate with Rez |

Git and Ansible remain explicit product terms.

## P0 - pilot readiness

| User story | Remediation | State |
|---|---|---|
| Understand the workflow | Apply Marcus-facing labels on the active Rez-hosted Netcode UI | Implemented in this slice |
| Know whether local execution is ready | Connector-local, no-device-session readiness for SSH/API, inventory, Ansible, and vendor collections | Implemented in this slice |
| Follow execution | Durable phase progress and per-device ordered activity | Existing and regression-tested |
| Recover safely | Retry failed devices, Investigate with Rez, rollback touched devices | Existing and regression-tested |
| Audit one change | Rez Change ID, device records, Git actions, proof, rollback, and activity | Existing and regression-tested |
| Use Git | Guided local/remote Git change history and exact change checkpoints | Existing and regression-tested |
| Use Ansible | Guided YAML and reviewed playbooks through the Local Connector | Existing and live-proven |
| Operate at catalog scale | Bounded search and rollout planning against 10,000 devices | Existing automated scale gate |

P0 exited on 2026-07-12 after a Marcus browser run proved the normal workflow,
the advanced Git/Ansible path, connector-local inventory and collection
readiness, and a zero-device-session capability check. The adversarial gate also
confirmed that an unknown connector fails closed and cannot enqueue work.

## P1 - production team operations

| User story | Remediation | Current boundary |
|---|---|---|
| Reuse AWX/AAP | Discover templates/surveys, launch through Netcode approval, import logs | Pending external-controller slice |
| Schedule work | Immediate, maintenance-window, future, and recurring schedules | Pending durable scheduler |
| Separate duties | Operator, approver, auditor, workflow author, administrator | Basic RBAC/tenant isolation exists; role depth pending |
| Plan upgrades | Approved versions, EOL/EOS, image readiness, compatibility | OS-upgrade workflow exists; lifecycle authority pending |
| Prove compliance | Golden standard, differences, exceptions, remediation history | Basic compliance exists; reporting depth pending |
| Search history | Device/site/change/workflow/engineer/date/result/RCA filters | Implemented and live-proven in this slice; bounded summaries keep full evidence on the per-change record only |
| Reuse successful work | Versioned team templates | Change templates exist; version lifecycle pending |
| Start from alerts | Monitoring/failed automation to read-only Rez RCA | Existing trigger contract; integration polish pending |

No P1 feature may be represented as available until its backend, permissions,
audit trail, and failure behavior have executable tests.

### P1 role-depth design

The production role model is capability-based rather than a simple numeric
hierarchy:

| Role | May do | Must not do |
|---|---|---|
| Observer | View inventory, Digital Twin, status, and non-sensitive history | Create or execute work |
| Auditor | Read and export approved change records, transcripts, and compliance history | Edit desired changes, approve, or push |
| Workflow author | Create templates, Git-backed desired changes, and Ansible workflows; run static validation | Approve or push production changes |
| Operator | Discover, plan, dry-run, run first-device tests, and push an already-approved change | Approve their own request or bypass policy |
| Approver | Review, approve, reject, or halt work | Modify the reviewed commands or approve their own request |
| Administrator | Manage users, policy, Local Connectors, retention, and integrations | Bypass requester-not-approver or erase immutable audit events |

The existing viewer/operator/admin model remains the only implemented model.
The deeper model cannot ship safely until the unified Rez shell forwards a
signed user, role, organization, method, and path to Netcode. A shared service
token alone must never become an administrator-equivalent identity.

## P2 - enterprise platform

| User story | Remediation | Current boundary |
|---|---|---|
| Survive service failure | Durable queues, resumable jobs, HA workers, duplicate-write protection | Architecture planned; single-worker pilot remains |
| Isolate customers | Organization/workspace isolation, per-customer connectors and retention | Core tenant isolation exists; deployment certification pending |
| Use enterprise identity | SSO and group-to-role mapping | Pending cloud deployment slice |
| Keep credentials private | Connector-local vault and rotation metadata | Core local boundary exists; Windows packaging/hardening pending |
| Use Infoblox later | Read IPAM/DNS/DHCP, reconcile, then governed reservations | Explicitly deferred; no placeholder integration |
| Extend workflows | Versioned workflow pack SDK, adapters, validators, form schemas | Internal contracts exist; public SDK pending |
| Report outcomes | Change, rollback, RCA, compliance, and trend reports | Core records exist; reporting product pending |
| Prove scale | 10,000-device catalog plus durable large rollout execution | Catalog gate exists; distributed execution certification pending |

### P2 foundation audit

- Organization-scoped records and cross-tenant 404 behavior are executable
  today and remain covered by regression tests.
- Local Connector credentials and inventory stay local; readiness returns only
  public capability metadata and opens zero device sessions.
- The 10,000-device catalog/search/rollout-planning gate is executable today.
- Single-worker service state, local user sessions, and non-distributed job
  execution remain pilot constraints, not enterprise claims.
- Infoblox remains intentionally absent. No Nautobot or NetBox DCIM/IPAM
  dependency is introduced by this plan.

## Execution order

1. Marcus language and Local Connector readiness.
2. P0 browser validation and regression gates.
3. Searchable production change history (complete) and signed role-depth implementation (pending unified identity propagation).
4. Scheduling and lifecycle authority.
5. AWX/AAP integration against a real controller.
6. HA, SSO, deployment isolation, and distributed scale.
7. Infoblox read-only integration after pilot demand.
8. Governed Infoblox writes and public workflow SDK only after read-path proof.

## Non-negotiable acceptance rules

- No placeholder card counts as an implementation.
- No capability check opens a device session.
- Git and Ansible are visible but optional in the normal workflow.
- Rez remains read-only and can only create a reviewed Netcode draft.
- Every production write requires the Netcode approval boundary.
- Customer credentials never enter the SaaS control plane.
- No lab hostname, IP address, or use-case-specific exception may enter production logic.
