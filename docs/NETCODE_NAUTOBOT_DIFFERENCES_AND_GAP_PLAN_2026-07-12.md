# Netcode and Nautobot: differences and gap plan - 2026-07-12

> Superseded for implementation by
> `NETCODE_MARCUS_P0_P2_EXECUTION_PLAN_2026-07-12.md`. Nautobot/NetBox DCIM/IPAM
> integration is no longer on the product roadmap; future address-management
> integration is explicitly Infoblox-first and deferred until after pilot proof.

## Executive decision

Netcode should not claim full Nautobot feature parity or rebuild Nautobot's mature
DCIM/IPAM/source-of-truth ecosystem. It should integrate with established sources
of truth and win on a narrower operating loop:

**discover -> plan -> validate -> first-device rollout -> verify -> Rez RCA ->
human-approved remediation**.

Nautobot Professional positions itself as a self-managed automation foundation
with discovery, operational tools, dashboards, Ansible/AWX integration, and
commercial support. Its Ansible app discovers AWX/AAP templates, mirrors surveys,
uses Nautobot RBAC/scheduling/history, and imports job logs. Its Git repository
model synchronizes content and secrets-backed remotes.

Official references:

- https://networktocode.com/nautobot/nautobot-professional/
- https://docs.nautobot.com/projects/core/en/stable/user-guide/platform-functionality/gitrepository/
- https://docs.nautobot.com/projects/ansible-automation/en/latest/user/app_overview/

## Grounded differences

| Capability | Nautobot | Netcode today | Decision |
|---|---|---|---|
| Network source of truth / DCIM / IPAM | Mature core category and ecosystem | Inventory, discovery, Digital Twin, source adapters; not full DCIM/IPAM | Integrate with Nautobot/NetBox rather than clone them |
| Discovery and operational tools | Discovery, ping, traceroute, SSH, dashboards | Runner discovery, multi-vendor collection, Shell, Digital Twin, application path | Keep differentiating around one customer-side access boundary |
| Git | Synced content repositories with provider secrets | Isolated per-change history with exact plan, commands, proof, and rollback artifacts | Keep automatic for normal users; add remote review workflow |
| Ansible | AWX/AAP template discovery, surveys, RBAC, scheduling, imported history | Guided YAML generation and runner-local `ansible-playbook` execution | Add AWX/AAP as an advanced provider, not a normal-user requirement |
| Workflow safety | Jobs, approvals, queues, history; edition-dependent apps | Exact preview, rollback, first-device rollout, waves, auto-halt, verification | Netcode strength; retain human write boundary |
| RCA after verification failure | Operational tools and ecosystem apps | Native read-only Rez RCA, scoped evidence, typed remediation draft | Primary differentiation |
| Lifecycle / EOL | Explicit lifecycle dashboards and enterprise apps | Basic platform/version inventory | Material product gap |
| RBAC, scheduling, HA, plugin ecosystem | Mature | Partial | Pilot-hardening gap; do not claim parity |

## Delivered in this slice

### Automatic Git change history

- one isolated history repository;
- one branch per selected change;
- exact artifact staging only;
- local Community mode or configured remote;
- unrelated workspace files cannot be committed by the API.

### Guided Ansible

- form-to-YAML generation without Python;
- explicit device targets;
- reviewed show/config modules for EOS, IOS/IOS-XE, NX-OS, and Junos;
- rollback mandatory for config;
- SHA-256 transport integrity;
- runner-side re-audit and runner-local credentials;
- real ORB runner check proven against an EOS device.

## Proposed parity plan for approval

### P0 - Pilot credibility

Status: current Git/Ansible slice complete.

1. Show change-history status and review link on every change record.
2. Stream Ansible task/device events into the same ordered activity log used by
   native Netcode workflows.
3. Add runner readiness for required binaries and vendor collections.
4. Keep all normal workflows independent of Git and Ansible knowledge.

Acceptance: Marcus can create a normal change without seeing Git/Ansible, then
open Advanced workflows to protect history or run a reviewed playbook.

### P1 - Existing automation teams

1. GitHub/GitLab/Bitbucket OAuth or customer-managed deploy credentials.
2. Push and pull-request links with review/merge status.
3. AWX/AAP controller provider using controller-side credentials.
4. Discover templates and surveys; render a guided Netcode form.
5. Launch through the existing approval boundary and import status, stdout, and
   job events into the Netcode change record.
6. Map controller inventory names to canonical Netcode device identities and
   assigned runners.

Acceptance: an existing AWX template can be selected, reviewed, approved,
executed, and audited without moving device credentials to the SaaS control
plane.

### P2 - Source-of-truth and lifecycle depth

1. Bidirectional governed adapters for Nautobot and NetBox.
2. Lifecycle/EOL, software compliance, warranty, and image readiness data.
3. Richer tenancy, location, circuit, prefix, VRF, VLAN, and relationship models.
4. Scheduled jobs, retention controls, enterprise RBAC, and HA workers.
5. Extension SDK for customer workflow packs.

Acceptance: Netcode consumes an enterprise source of truth without becoming a
second conflicting source, and writes back only governed operational outcomes.

## GTM implication

Do not sell "Nautobot parity." Sell faster adoption for CLI-native teams and the
closed loop Nautobot does not natively make Netcode's core story: exact change,
live verification, read-only RCA for the exception, and a human-approved scoped
remediation. For Nautobot customers, position Netcode + Rez as an execution and
diagnostics layer using Nautobot as the source of truth.
