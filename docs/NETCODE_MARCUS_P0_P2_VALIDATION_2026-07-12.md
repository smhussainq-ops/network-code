# Netcode Marcus P0-P2 validation - 2026-07-12

## Verdict

- **P0 pilot workflow: GO.** Marcus can understand the normal workflow, verify
  the Local Connector without opening device sessions, and optionally use Git
  and guided Ansible.
- **Supported P1 slice: GO.** Production change history is tenant-scoped,
  bounded, searchable, paginated, and linked to full per-change evidence.
- **P2 enterprise platform: NOT complete.** The tested foundations are sound,
  but HA, SSO, distributed execution, signed cross-service identity, Windows
  hardening, and deployment certification remain required.

## Checkpoints

| Repository | P0 commit | P1 commit |
|---|---|---|
| Netcode | `ce0d844` | `ff58bd9` |
| Rez | `d844c57` | `062efac` |

## Marcus browser proof

The signed-in Rez-hosted Netcode screen on `:4005` showed:

- `Local Connector: 1 online`
- 26 catalog and local-inventory devices
- SSH and API available
- Ansible installed, with `arista.eos` detected
- capability check opened zero device sessions
- normal language: desired change, first-device test, rollout group, push
  change, confirm result, and change record
- Git change history and guided Ansible available only under Advanced
  workflows

The change-history view initially returned `1-10 of 129`, then a Marcus filter
for `HQ` plus source `Rez RCA` returned exactly two validated HQ remediation
records. The list opened no device sessions. Selecting a row remains the only
path to its full commands, validation, proof, rollback, and activity.

## Adversarial findings and repairs

1. The first browser load retained stale pre-restart `404` state. A real reload
   proved the authenticated proxy route and correct Local Connector data; no
   auth bypass was added.
2. The original history endpoint returned each complete result, including
   rendered commands and intent YAML. It was replaced with a bounded summary.
   A live two-row response is approximately 1.4 KB; full evidence stays behind
   the per-change record endpoint.
3. Older records had no indexed source column. Compatibility filters recognize
   historical Rez and Ansible records from their existing durable metadata
   without rewriting the audit record.
4. An unknown Local Connector returns `404` and cannot enqueue a capability
   job.
5. New history indexes are organization-first; API queries always bind the
   authenticated organization before applying user filters.

## Automated validation

| Gate | Result |
|---|---|
| Netcode full suite | 232 passed |
| Rez Netcode/RCA/trigger contracts | 147 passed |
| Focused Netcode UI progression tests | 4 passed |
| Netcode wizard ESLint | passed |
| Vite production bundle | passed, 2,860 modules |
| 10,000-device catalog/search/rollout planning | passed inside Netcode suite |
| Unknown connector fail-closed | passed live and automated |
| Cross-tenant change isolation | passed |

The repository-wide TypeScript type-check remains blocked by unrelated existing
errors in Assessment, Incident, Packet Analysis, and Digital Twin files. The
active Netcode wizard passes focused ESLint and Vite bundling and is not listed
in that error set.

## Product boundary

- Netcode owns discovered operational inventory, topology, desired changes,
  execution, verification, drift, and change records.
- Rez remains read-only and may create only a reviewed Netcode draft from a
  confirmed structured RCA.
- Nautobot and NetBox DCIM/IPAM are not part of the roadmap or surfaced provider
  catalog.
- Infoblox is explicitly deferred and read-first. No placeholder integration is
  counted as implemented.

## Required next slices

1. Signed Rez-to-Netcode user/role/organization propagation, followed by the
   capability-based Observer, Auditor, Workflow author, Operator, Approver, and
   Administrator model.
2. Durable maintenance-window and recurring scheduling.
3. Lifecycle authority for approved releases, EOL/EOS, image compatibility,
   and upgrade readiness.
4. AWX/AAP integration against a real controller.
5. HA queues/workers, duplicate-write protection, SSO/group mapping, Windows
   Local Connector packaging and hardening, and distributed scale proof.
6. Infoblox read-only reconciliation only after pilot demand.
