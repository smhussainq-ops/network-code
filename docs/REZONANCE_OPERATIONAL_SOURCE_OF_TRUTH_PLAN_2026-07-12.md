# Rezonance Operational Source of Truth Plan

**Date:** 2026-07-12

**Status:** Implemented and validated for the pilot foundation

**Scope:** Netcode, Rez Diagnostics, Digital Twin, Shell, and the Local Connector
**Implementation status:** Slices 1-8 are implemented across Netcode and Rez. The authority, repository, import, compiler, observation, Git lifecycle, native-consumer, UI, scale, rollback, and Marcus end-to-end gates are covered by tests and live proof. Optional NetBox, Infoblox, and controller-authority connectors remain deliberately deferred.

## Implementation outcome

The native Rezonance Network Model is now the shared operational authority for Netcode, Rez Diagnostics, Digital Twin, and Shell.

| Slice | Outcome | Primary checkpoint |
|---|---|---|
| 1 | Domain authority, revision, observation, and no-secret contracts | `6bc2c63` |
| 2 | Durable SQLite/PostgreSQL-ready model repository and bounded queries | `cb36613` |
| 3 | Identity-safe, idempotent model imports and conflict handling | `d55ca6c` |
| 4 | Deterministic effective-intent compiler with explicit unknown coverage | `6718948` |
| 5 | Fresh observation ingestion and approved-versus-observed reconciliation | `4a77da3` |
| 6 | Human approval, isolated Git history, verified activation, and model rollback | `7d55bbe` plus final lifecycle commit |
| 7 | Model-scoped planning and native Rez/Digital Twin/Shell consumers | `3690721`, `c4ef697`, plus final consumer commits |
| 8 | Network Model UI, RBAC, scale, Marcus E2E, and adversarial rollback recovery | final Netcode and Rez commits |

Detailed evidence, exact IDs, test counts, and residual test debt are recorded in `REZONANCE_OPERATIONAL_SOURCE_OF_TRUTH_VALIDATION_2026-07-12.md`.

## Executive decision

Rezonance should ship with its own **Operational Source of Truth**, called the **Rezonance Network Model** in the product UI.

It should make Rezonance work end to end without requiring NetBox, Nautobot, Infoblox, or a CMDB. It should not attempt to replace those products' full DCIM, IPAM, DNS, DHCP, rack, power, procurement, or circuit-management functions.

The product promise is:

> Discover once. Approve the network model. Automate and diagnose from the same context.

The model must answer the operational questions required by Netcode and Rez:

- What devices, sites, roles, interfaces, links, routing domains, and application dependencies exist?
- What is live now, and how fresh is that observation?
- What has an engineer explicitly approved as intended state?
- What differs between live and intended state?
- Which change, investigation, verification, rollback, and Git checkpoint produced the current state?

## Why this is needed

The current implementation has strong pieces, but they are separate:

| Existing component | Current role | Gap |
|---|---|---|
| Netcode `device_catalog` and `device_aliases` | Durable identity, site, role, platform, runner assignment, and scalable search | Does not hold the complete approved network design |
| Netcode local YAML source of truth | Inventory, policies, templates, and known subnets | File-oriented and not a versioned enterprise model |
| Netcode Git intent/change records | Plan, commands, approval, apply, verify, rollback, and audit | Represents changes, not the complete long-lived network design |
| Rez `network_design.yaml` and `network_design_context.py` | Strict approved design, coverage, routing boundaries, reachability, and operational dependencies | Static file and separate from Netcode inventory/change state |
| Digital Twin live/snapshot/approved views | Operational topology and historical observations | Has separate baseline and site-assignment paths |
| Rez incidents and RCA | Fresh evidence, deterministic findings, and remediation proposals | Must never become intended state without approval and successful verification |

The immediate objective is not to add another store. It is to make these components use one authority contract.

### Code-grounded starting points

The plan is based on these current implementation boundaries:

- Netcode `netcode/store.py` already defines the durable `device_catalog`, `device_aliases`, runner assignment, changes, rollouts, execution events, Shell sessions, and transcripts.
- Netcode `netcode/source_of_truth.py` currently serves local YAML and contains an incomplete direct NetBox-to-YAML sync path that must not become the canonical architecture.
- Netcode discovery and import paths in `netcode/discovery.py` and `netcode/api.py` already sanitize public inventory facts and reject cloud-side credentials in runner mode.
- Rez `sdk_tools/network_design_context.py` already enforces approved source, human approval, domain coverage, exact dependencies, no secrets, and the rule that discovery is observation only.
- Rez `config/network_design.yaml` is the current approved-design artifact and provides a migration fixture, not a scalable serving store.
- Rez Digital Twin server/UI code already distinguishes current observations, historical snapshots, and operator-approved baselines; that separation must be preserved when the storage path is unified.

These are reused and consolidated. The plan does not replace proven approval, read-only RCA, Local Connector, or Git/change-history boundaries.

## Architectural ownership

### Netcode control plane owns the model

Netcode already owns organization/environment identity, runner enrollment, device catalog, discovery jobs, change records, approval, execution, verification, rollback, and Git history. The canonical Network Model therefore belongs in the Netcode control plane.

### Rez is a read-only consumer

Rez receives:

- the approved effective model for the investigation scope;
- fresh observations collected through the Local Connector;
- declared coverage and unknown domains;
- related change and verification records.

Rez may create a typed remediation **proposal**. It cannot modify approved intent or apply configuration.

### Digital Twin is a view, not a competing source

The Digital Twin renders:

- the approved model;
- the current observation overlay;
- historical observations;
- differences, incidents, changes, and application paths.

Moving nodes changes layout metadata only. Editing topology or dependency intent creates a reviewed model revision; it does not silently mutate live or approved state.

### The Local Connector owns credentials and device access

Device, controller, Git-provider, and future external-source credentials stay customer-side. The SaaS control plane receives normalized facts, approved intent, job results, and policy-permitted evidence, never device credentials.

## The three truth planes

The implementation must keep these planes physically and semantically separate.

### 1. Observed state

What discovery, SSH, API, telemetry, or a controller observed.

Required metadata:

- `observed_at` and `expires_at`;
- collector and Local Connector identity;
- device and environment identity;
- source protocol and evidence reference;
- normalization version;
- freshness and validation grade;
- anonymization/privacy policy applied.

Observed state is never automatically approved intent.

### 2. Approved intent

What an authorized engineer approved as the intended operational design.

Required metadata:

- immutable revision ID;
- parent revision;
- author, reviewer, and approval timestamp;
- declared domain coverage;
- provenance/import source;
- model diff;
- Git checkpoint;
- lifecycle state.

Approved intent is partial by design. If routing is covered but QoS is not, Rezonance may evaluate routing but must report QoS as `UNKNOWN`, not healthy.

### 3. Operational history

What Netcode and Rez did around the model:

- discovery/import jobs;
- proposed and approved model revisions;
- change plans and exact commands;
- dry-run, approval, apply, verify, and rollback events;
- Rez incidents, evidence, conclusions, and remediation drafts;
- drift and reconciliation findings;
- Shell sessions and transcripts.

History is immutable audit evidence, not active intent.

## Authority is domain-specific

Rezonance must not use one global provider-precedence list. Each domain has an explicit authority binding.

| Domain | Standalone default | Optional future authority | Conflict behavior |
|---|---|---|---|
| Device identity and runner assignment | Rezonance approved catalog | NetBox, CMDB | Hold conflict for review; never merge ambiguously |
| Site, role, and archetype | Rezonance approved model | NetBox, CMDB | Proposed diff only |
| Interfaces, neighbors, routes, sessions | Fresh Local Connector observation | Controller/telemetry | Newest valid observation; keep provenance |
| Address plan and prefix ownership | Rezonance approved model | Infoblox/IPAM | External values proposed or authoritative per configured domain |
| Routing and redistribution intent | Rezonance approved model | Git import | Explicit approval required |
| Firewall, NAT, SD-WAN, VPN, HA intent | Rezonance approved model | FortiManager/Panorama/controller | Controller execution status is supporting evidence, not end-to-end success |
| Golden standards | Rezonance approved model | Git/Ansible repository | Versioned candidate and approval required |
| Change/execution state | Netcode | None | Netcode remains authoritative |
| RCA and remediation recommendation | Rez | None | Advisory until converted to an approved Netcode change |

No source may silently overwrite another. Conflicts are first-class records with an owner, status, and resolution.

## Canonical model

The serving model should extend the existing `PlatformStore` abstraction so Community can use SQLite and SaaS can use PostgreSQL.

### Core entities

| Entity | Purpose |
|---|---|
| Organization and environment | Tenant and operational namespace |
| Model revision | Immutable candidate/approved/superseded revision |
| Authority binding | Domain-specific source and precedence policy |
| Site archetype | Reusable design for store, branch, campus, data center, cloud edge, and other patterns |
| Site | Site identity, region, criticality, tags, and archetype binding |
| Device | Stable canonical UUID plus display identity, platform, role, site, and Local Connector assignment |
| Device alias/source binding | Hostname, IP, serial, controller ID, external ID, and prior names |
| Interface and address | Intended identity, role, addressing, VRF, and criticality |
| Link and adjacency | Physical/L2, routing, tunnel, controller, and service dependencies |
| Address plan and prefix class | Site prefixes, loopbacks, transit ranges, service ranges, and ownership |
| Routing domain | Protocol, process, area/level, ASN, VRF, and expected membership |
| Redistribution boundary | Exact protocol exchange, policy, prefix class, and redundancy requirements |
| Operational dependency | Interface, LLDP, OSPF, BGP, default route, SD-WAN, QoS, firewall, NAT, VPN, and HA expectation |
| Application path | Source/destination/protocol/service intent and expected forward/return dependencies |
| Golden standard | Versioned baseline inherited by archetype, site, role, group, and device |
| Observation | Append-only normalized live fact with freshness and provenance |
| Reconciliation finding | Observed-versus-approved difference or source conflict |
| Change/incident linkage | Netcode change ID, Rez incident ID, verification, rollback, and audit references |

### Inheritance

Large deployments cannot duplicate every expectation per device. Effective intent is compiled deterministically:

```text
organization standard
  -> site archetype
  -> site override
  -> role/device-group override
  -> device exception
```

Every override is explicit, versioned, and visible in the effective-model view. There are no hidden lab defaults or hostname-specific production rules.

### Stable identity and deduplication

Every device receives an immutable internal UUID. Matching may use approved aliases, serial number, management address, hostname, and controller ID, but:

- case and format normalization is deterministic;
- an unknown platform never defaults to a vendor;
- an ambiguous match creates a conflict instead of merging records;
- deletion is a retired/superseded state, not destructive loss of history;
- reconnecting a Local Connector does not create duplicate devices.

## Revision and change lifecycle

### Model lifecycle

```text
proposed -> in_review -> approved -> active -> superseded
                       \-> rejected
```

Discovery and imports create `proposed` facts or candidate revisions only.

### Closed-loop change lifecycle

1. An engineer, drift finding, workflow pack, or confirmed Rez RCA creates a candidate model revision.
2. Netcode calculates the exact delta from the active approved revision.
3. Netcode generates commands, rollback, scope, and verification checks.
4. Dry-run and policy gates execute.
5. A human approves the write; requester-not-approver remains available.
6. Netcode applies through the assigned Local Connector.
7. Netcode verifies actual state against the candidate revision.
8. Only a successful verification promotes the candidate revision to `active`.
9. A failure leaves the prior revision active and triggers read-only Rez investigation.
10. Rollback restores the previous approved state and verifies it before closure.

This prevents the database from claiming a failed change is intended or healthy.

## Git and database roles

The database and Git solve different problems.

### Database

- serves indexed, tenant-scoped model queries;
- supports 5,000-10,000+ devices without loading full YAML files;
- stores current materialized state, revision metadata, conflicts, and links;
- uses PostgreSQL in SaaS and SQLite in Community/local mode.

### Git

- stores every approved model revision and human-readable diff;
- protects change, verification, and rollback checkpoints;
- supports GitHub, GitLab, Bitbucket, or a local Community repository;
- remains optional to operate manually: Netcode creates branches and commits automatically.

Suggested artifact structure:

```text
network-model/<environment>/
  active.yaml
  revisions/<revision-id>/model.yaml
  revisions/<revision-id>/diff.md
  revisions/<revision-id>/approval.json
  revisions/<revision-id>/verification.json
```

Git is not queried for every runtime read. PostgreSQL/SQLite is the serving layer, and Git is the durable review/audit layer.

## Product experience for Marcus

### First-run flow

1. Install and enroll the Local Connector.
2. Add seed addresses or import a public inventory file.
3. Discover devices and topology without sending credentials to SaaS.
4. Review identity conflicts, site groupings, roles, and unsupported devices.
5. Select or create site archetypes and golden standards.
6. Review the proposed network model and declared coverage.
7. Approve revision 1; Netcode creates the Git checkpoint automatically.
8. Open Digital Twin, Shell, a workflow pack, or Rez using the same inventory and context.

### Network Model UI

Add one primary surface named **Network Model** with:

- **Overview:** coverage, freshness, unresolved conflicts, active revision, and recent changes;
- **Sites:** archetypes, roles, address plans, dependencies, and health scope;
- **Devices:** paginated search, aliases, runner assignment, platform, role, and lifecycle;
- **Dependencies:** interfaces, links, routing, SD-WAN, firewall/NAT, QoS, VPN, HA, and application paths;
- **Standards:** golden configurations and inheritance;
- **Differences:** observed versus approved, grouped by risk and scope;
- **Revisions:** plain-language changes, approval, Git checkpoint, and rollback.

UI language should explain the engineering outcome first. Git and Ansible remain visible in **Advanced workflows**, but Marcus should not need to understand branching or write Python/Ansible to use the normal workflow.

## APIs and events

Introduce versioned, tenant-scoped APIs under `/api/network-model`:

- query active/effective model by environment, site, device, domain, or application flow;
- create candidate revisions from UI, YAML/CSV import, discovery proposals, drift, or Rez;
- review, approve, reject, supersede, and compare revisions;
- query observations with freshness and provenance;
- query conflicts and reconciliation findings;
- export/import the versioned model;
- link change, incident, verification, rollback, and Git records.

All list APIs use cursor pagination, filters, bounded result sizes, and stable sort order. Model-change events notify Digital Twin, Netcode planners, Rez context builders, and drift workers without each product maintaining another copy.

## Migration from the current implementation

Migration must be additive and reversible.

1. Keep the existing `device_catalog` and `device_aliases` as the identity foundation.
2. Add model-revision, domain, observation, conflict, and linkage tables without replacing current APIs.
3. Import local YAML inventory as a candidate snapshot with source provenance.
4. Import the current approved `config/network_design.yaml` as the first approved design revision for its environment.
5. Import operator-created site assignments as proposals; require one explicit review before they become active intent.
6. Keep Digital Twin snapshots as observations. An approved Twin snapshot may support a baseline, but it does not silently define uncovered design domains.
7. Link existing Netcode applied intents and changes as history; do not assume every historical apply represents the current complete design.
8. Run old and new read paths in shadow mode and compare results.
9. Switch Digital Twin, Netcode, and Rez consumers one at a time behind feature gates.
10. Remove duplicate file/store paths only after parity and rollback tests pass.

## Implementation slices

Each slice is test-gated and separately committed. An adversarial review is required before moving to the next slice.

### Slice 1: Authority ADR and contracts

- Freeze terminology, ownership, lifecycle, domain coverage, and conflict behavior.
- Define versioned schemas for model revisions, observations, effective context, and reconciliation findings.
- Replace hardcoded provider assumptions with a domain authority registry.
- Add no-secret and tenant-isolation contract tests.

**Gate:** discovery, an incident, or a stale observation cannot become approved intent through any API.

### Slice 2: Durable model repository

- Extend `PlatformStore` for revisions, sites, dependencies, standards, observations, conflicts, and links.
- Support SQLite and PostgreSQL migrations.
- Add indexed, cursor-paginated query paths.

**Gate:** 10,000-device synthetic model queries remain bounded and do not load the entire model.

### Slice 3: Identity and migration

- Reuse `device_catalog` and aliases.
- Add deterministic deduplication and source bindings.
- Import local YAML, approved Rez design, site assignments, and historical references without destructive edits.

**Gate:** ambiguous identities fail closed; repeated imports are idempotent.

### Slice 4: Effective-intent compiler

- Compile organization, archetype, site, role/group, and device layers.
- Preserve declared coverage and unknown domains.
- Produce the exact deterministic context currently expected by Rez design validators.

**Gate:** no site-specific or lab-specific constants exist in compiler code; fixtures supply all design data.

### Slice 5: Observation and reconciliation engine

- Ingest normalized discovery, SSH/API, telemetry, and controller observations.
- Enforce freshness, provenance, validation grade, and retention.
- Compute observed-versus-approved deltas and source conflicts.

**Gate:** failed scans and incidents never update baselines; stale data cannot certify health or authorize a change.

### Slice 6: Git and Netcode lifecycle integration

- Create candidate revisions from normal plans and Rez remediation drafts.
- Commit approved revisions, diffs, verification, and rollback evidence.
- Promote the model only after successful verification.

**Gate:** failed apply/verify leaves the prior active revision unchanged; rollback restores and verifies the previous revision.

### Slice 7: Native consumers

- Digital Twin renders approved model plus live/historical overlays.
- Netcode target selection, plans, drift, and verification use effective intent.
- Rez pre-agent context uses the same model and exact fresh observations.
- Shell resolves devices through the same canonical identity and Local Connector assignment.

**Gate:** one discovered device has one canonical identity across all four surfaces.

### Slice 8: Marcus UI, scale, and end-to-end proof

- Build the Network Model review/approval UI.
- Add conflict resolution, revision comparison, coverage, provenance, and freshness views.
- Test desktop and mobile accessibility.
- Run scale, tenant, connector replay, failure, and recovery tests.

**Gate:** complete the real-user scenario below without editing YAML or invoking Python/Ansible manually.

### Priority mapping

| Priority | Required outcome | Slices |
|---|---|---|
| P0: authority foundation | One identity, one authority contract, safe migration, deterministic effective intent | 1-4 |
| P1: closed-loop operation | Fresh observations, reconciliation, Git/change lifecycle, and native product consumers | 5-7 |
| P1: pilot usability and proof | Marcus UI, tenant/security validation, scale tests, and end-to-end proof | 8 |
| P2: ecosystem expansion | Infoblox, NetBox/CMDB, controller enrichment, and governed write-back when demanded | After native model acceptance |

No P2 connector work should delay or weaken the P0/P1 standalone product.

## End-to-end acceptance scenario

1. Marcus enrolls one Local Connector.
2. He discovers a multi-vendor site and sees proposed devices, links, sites, and roles.
3. He resolves one identity conflict and corrects one site assignment.
4. He applies a reusable site archetype and approves the first model revision.
5. Digital Twin renders the same approved topology with a fresh-state overlay.
6. He creates a two-device change from a workflow pack.
7. Netcode shows exact commands, rollback, affected dependencies, and verification checks.
8. Dry-run passes, another engineer approves, and the first device is applied.
9. Verification fails against one declared application dependency.
10. Rez receives the exact change, model revision, path, site, and fresh evidence; it remains read-only.
11. Rez confirms a typed root cause and creates a scoped Netcode remediation draft.
12. Marcus reviews, approves, applies, and verifies the remediation.
13. The candidate model becomes active only after verification.
14. Digital Twin, change history, Git checkpoint, Rez incident, Shell transcript, and rollback all reference the same canonical IDs.

## Security and adversarial requirements

- Every record is scoped by organization and environment.
- No password, token, private key, community string, or credential-shaped field enters the model.
- Local Connector and external-system credentials remain local.
- Raw configurations are not accepted as model intent without parsing, redaction, schema validation, and review.
- Model approval requires authenticated user identity and immutable audit metadata.
- Rez identities and remediation proposals cannot impersonate a human approver.
- Replayed or duplicate connector events are idempotent.
- A compromised observation source cannot overwrite approved intent.
- Source deletion does not cascade-delete approved history.
- Cross-tenant aliases, revisions, observations, and searches are prohibited by tests.
- Anonymization policy is recorded on every exported observation/evidence artifact.

## Scale requirements

Pilot and production design targets:

- 10,000+ devices per environment;
- paginated search returning at most 50 records by default;
- zero device connections during initial UI render;
- append-only time-series observations with retention/compaction;
- materialized current-state tables for fast UI and planner reads;
- async import/reconciliation jobs with progress and resumability;
- partitioning/indexing by tenant, environment, device, domain, and time;
- bounded Digital Twin views by site, path, layer, or query rather than rendering the full fleet by default.

## Integrations strategy

### Pilot

Rezonance is standalone. Support:

- Local Connector discovery;
- CSV/YAML import and export;
- Git-backed approved revisions;
- controller/device observations through existing adapters.

Do not require or promote NetBox as a prerequisite.

### Later, only when customer demand is proven

1. **Infoblox read-first:** prefixes, allocations, DNS, and DHCP as an optional domain authority.
2. **NetBox/CMDB read-first:** optional device/site/role enrichment for customers already using it.
3. **Controller integrations:** FortiManager, Panorama, wireless, SD-WAN, and cloud controllers for policy/execution facts.
4. **Governed write-back:** only after conflict, approval, idempotency, and rollback semantics are proven.

All connector traffic runs through the Local Connector over outbound TLS. The current prototype NetBox sync must not be treated as pilot-ready because it directly merges into local YAML and lacks the authority/reconciliation workflow described here.

## Explicit non-goals

Do not build these before customer demand proves they are necessary:

- rack elevations, power chains, procurement, and asset lifecycle;
- full cable plant and facilities management;
- authoritative DHCP/DNS services;
- enterprise IP allocation workflows equivalent to a dedicated IPAM;
- a general-purpose CMDB;
- NetBox/Nautobot plugin ecosystems;
- silent auto-learning of intent from production state;
- autonomous production remediation.

## Definition of done

The source-of-truth foundation is complete only when:

- Netcode, Rez, Digital Twin, and Shell resolve the same canonical identity;
- approved intent and observations are separate in storage, API, and UI;
- every health/RCA claim shows model coverage, provenance, and freshness;
- discovery and failed incidents cannot mutate approved intent;
- every approved revision has a human, timestamp, diff, and Git checkpoint;
- failed changes do not promote candidate intent;
- successful verification promotes the intended revision atomically;
- rollback restores both live state and the prior active model;
- 10,000-device scale and tenant isolation tests pass;
- the Marcus scenario passes end to end without lab-specific constants or manual scripting.

## Completed execution and next boundary

Slices 1-8 were executed in order and the native authority, revision, reconciliation, consumer, scale, and rollback contracts are now in place. Do not begin a NetBox or Infoblox connector until a customer requires it. Any future connector must enter through the same proposal, authority, conflict, approval, freshness, and audit contracts rather than becoming another competing store.
