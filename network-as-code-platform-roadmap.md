# Network-As-Code Platform Roadmap

## Purpose

This roadmap turns the current Arista lab slice into a production-grade network-as-code platform, then expands it into the broader infrastructure transformation capability shown in the target feature set:

- Large-scale data center and backbone automation
- Multi-vendor network lifecycle management
- Build-and-test labs for operational validation
- Source-of-truth-driven infrastructure changes
- Observability, telemetry, drift, and compliance
- AI/ML-assisted predictive operations
- Cloud, edge, and infrastructure cost optimization

The platform should stay simple for engineers:

> Request the outcome, see the proof, approve only when safe, and keep every artifact visible.

## Current Baseline

The current platform has a working Arista lab vertical slice:

- Guided UI and CLI for an `add_vlan` workflow.
- Intent YAML generation.
- Jinja rendering into Arista EOS configuration.
- Static validation with fail-closed policy checks.
- Live outcome panel and action journal for click-by-click transparency.
- Arista EOS config-session dry-run, apply, verify, and rollback.
- Durable job and change records.
- Platform capability endpoint exposing the 15 core deliverables.
- Rez adapter bridge for state collection where Rez dependencies are available.
- ORB VM containerlab validation path for the Arista lab.

This is the proof point. The roadmap below turns it into a scalable platform.

## Platform Outcome

The final platform should let an engineer or service owner do this safely:

1. Describe the desired network or infrastructure outcome.
2. Resolve that request against source of truth.
3. Generate vendor-specific candidate artifacts.
4. Validate policy, blast radius, dependencies, and risk.
5. Test the candidate in a lab, simulator, or pre-production environment.
6. Require approval when risk or scope demands it.
7. Apply through controlled adapters.
8. Verify live state.
9. Detect drift.
10. Record evidence, reports, rollback, and audit history.

## Roadmap Phases

### Phase 0: Arista Lab Proof

Status: mostly complete.

Goal:

Prove the network-as-code operating model end to end with one safe workflow and one vendor lab.

Delivered:

- Add VLAN workflow.
- Arista EOS lab adapter.
- Static validation.
- Live UI transparency.
- Rollback.
- Job records.
- Basic source-of-truth files.
- Reports.

Exit criteria:

- Safety check passes and blocks unsafe input.
- Dry-run proves candidate config without committing.
- Apply verifies VLAN presence.
- Rollback verifies VLAN absence.
- UI shows expected and actual outcome for every click.

### Phase 1: Platform Foundation Hardening

Goal:

Make the current lab slice stable, understandable, and extensible before adding more workflows.

Key deliverables:

- Workflow engine with explicit states: requested, rendered, validated, dry-run, approved, applied, verified, rolled back, failed.
- First-class artifact model for intent, template, rendered config, validation, diff, job, report, and device evidence.
- Better UI information architecture:
  - simple outcome-first view
  - technical evidence drawer
  - action journal
  - source-of-truth view
  - job history
  - rollback history
- Stronger test coverage:
  - unit tests for validators
  - API contract tests
  - browser workflow tests
  - lab adapter contract tests with mocked devices
- Better error messages:
  - what failed
  - why it matters
  - what to fix
  - whether any device was touched
- Packaged local development and ORB lab setup.

Acceptance criteria:

- A new engineer can run the platform locally in under 15 minutes.
- Every UI action has expected outcome, actual outcome, and evidence.
- No device write can occur without validation and dry-run proof.
- The platform remains useful even when the lab is unreachable.

### Phase 2: Production Safety Controls

Goal:

Add the controls required before any production network can be targeted.

Key deliverables:

- Authentication and role-based access control:
  - requester
  - reviewer
  - approver
  - operator
  - platform admin
- Approval workflow:
  - low-risk auto-approval
  - high-risk human approval
  - emergency path with stronger audit
- Secrets management:
  - no device passwords in YAML
  - integration path for Vault, AWS Secrets Manager, or enterprise secret store
  - per-environment credentials
- Environment separation:
  - lab
  - pre-production
  - production
- Policy-as-code:
  - segmentation rules
  - management access protections
  - allowed change windows
  - blast-radius limits
  - platform-specific guardrails
- Audit and evidence:
  - immutable job logs
  - user attribution
  - approval records
  - rollback records
  - signed reports
- Production rollback framework:
  - pre-change snapshot
  - generated rollback plan
  - tested rollback when possible
  - post-rollback verification

Acceptance criteria:

- Production apply is impossible without identity, authorization, approval, and required evidence.
- Secrets are never stored in repo or user input artifacts.
- Every production change can answer: who requested, who approved, what changed, what proof exists, and how to roll back.

### Phase 3: Source Of Truth And GitOps

Goal:

Make source of truth and Git the control plane for all change intent.

Key deliverables:

- Source-of-truth integration:
  - NetBox or Nautobot for devices, sites, interfaces, prefixes, VLANs, circuits, and tenants
  - CMDB or ServiceNow for business ownership and change tickets
  - IPAM validation for subnets and VLAN allocation
- Git workflow:
  - branch per change
  - pull request per change
  - generated diffs
  - policy checks in CI
  - signed commits where required
- Intent registry:
  - reusable intent types
  - schema versioning
  - migration path for older intents
- Template registry:
  - vendor templates
  - platform templates
  - reusable snippets
  - template testing
- Drift detection baseline:
  - compare intended state, rendered config, and live state
  - identify unmanaged drift
  - open remediation workflow

Acceptance criteria:

- The platform does not guess inventory or policy.
- Every generated config is traceable to source-of-truth data and Git history.
- A pull request can show the complete network impact before execution.

### Phase 4: Multi-Vendor And 25k Device Scale

Goal:

Evolve from an Arista lab slice to a scalable multi-vendor execution platform.

Target vendors and platforms:

- Arista EOS
- Cisco IOS XE / NX-OS
- Juniper Junos
- Fortinet FortiGate
- Palo Alto PAN-OS
- Cumulus Linux or SONiC where relevant
- Cloud network APIs where relevant

Key deliverables:

- Adapter SDK:
  - render contract
  - validate contract
  - dry-run contract
  - apply contract
  - verify contract
  - rollback contract
  - collect-state contract
- Vendor capability matrix:
  - supports candidate config
  - supports config sessions
  - supports commit confirmed
  - supports rollback
  - supports structured diff
  - supports telemetry
- Distributed worker system:
  - queue-backed execution
  - concurrency limits
  - per-site throttling
  - per-vendor throttling
  - retries with safety rules
  - idempotency keys
- Durable platform storage:
  - PostgreSQL for jobs, approvals, artifacts, and audit
  - object storage for reports and evidence
  - cache layer for source-of-truth snapshots
- Scale architecture:
  - shard by site, region, or device group
  - worker pools close to network regions
  - backpressure controls
  - bulk-change batching
  - partial failure handling
- Device safety:
  - pre-checks
  - health checks
  - lock per device
  - config drift preflight
  - post-change verification

Acceptance criteria:

- The platform can model and schedule changes for 25k+ devices without blocking the UI.
- Execution is asynchronous, observable, retry-safe, and auditable.
- A failed device does not blindly cascade failure across a large batch.
- Multi-vendor workflows use the same platform contract even when vendor mechanics differ.

### Phase 5: Observability, Telemetry, And Drift

Goal:

Make the platform continuously aware of live network state.

Key deliverables:

- State collection:
  - periodic snapshots
  - on-demand snapshots
  - pre-change and post-change snapshots
  - vendor-normalized state model
- Telemetry ingestion:
  - interfaces
  - routing
  - BGP/OSPF/EVPN state
  - VLANs and VRFs
  - device health
  - errors and discards
  - latency and loss where available
- Drift engine:
  - intended versus live state
  - template-rendered versus running config
  - source-of-truth versus discovered state
  - approved versus unapproved drift
- Observability UI:
  - device state
  - site health
  - change health
  - drift view
  - compliance posture
- Alert and incident integration:
  - ServiceNow
  - PagerDuty
  - Slack or Teams
  - webhook output

Acceptance criteria:

- The platform can prove whether the network still matches intended state after the change window.
- Drift is visible, explainable, and tied to remediation workflows.
- Operators can see change impact and network health in one place.

### Phase 6: Data Center, Cloud, Edge, And Backbone Automation

Goal:

Expand from network device changes into broader infrastructure transformation.

Key deliverables:

- Data center fabrics:
  - EVPN/VXLAN workflow support
  - tenant/VRF onboarding
  - leaf/spine interface lifecycle
  - service insertion
  - fabric compliance
- Backbone and WAN:
  - BGP peer lifecycle
  - prefix policy changes
  - traffic-engineering guardrails
  - circuit turn-up workflows
  - PoP/edge rollout templates
- Cloud networking:
  - VPC/VNet intent
  - transit gateway / cloud router workflows
  - cloud firewall policy lifecycle
  - hybrid connectivity validation
- Infrastructure provisioning:
  - Terraform integration where infrastructure APIs are the control plane
  - Ansible or Python where device APIs are the control plane
  - Kubernetes networking workflows where relevant
- Lab and digital twin:
  - generated containerlab topologies
  - pre-production simulation
  - synthetic traffic validation
  - golden-path regression tests

Acceptance criteria:

- The platform supports more than individual device config. It supports service-level outcomes.
- Lab and pre-production validation become part of the normal change lifecycle.
- Backbone, edge, cloud, and data center changes share the same evidence model.

### Phase 7: AI/ML-Assisted Operations

Goal:

Use AI and ML to help engineers understand risk, detect patterns, and recommend safe actions without bypassing deterministic controls.

Key deliverables:

- Natural-language change intake:
  - translate a request into proposed intent
  - require human review before execution
  - show the generated YAML and policy impact
- Risk scoring:
  - blast radius
  - affected services
  - historical failure patterns
  - change-window risk
  - device health risk
- Predictive operations:
  - anomaly detection
  - capacity trend detection
  - interface error prediction
  - control-plane instability signals
- Assistant workflows:
  - explain failed validation
  - suggest safe fixes
  - summarize diff and blast radius
  - generate change review notes
  - generate rollback runbooks
- Guardrails:
  - AI cannot directly apply changes
  - AI recommendations must be traceable
  - deterministic validators remain the source of enforcement

Acceptance criteria:

- AI improves operator speed and understanding, but cannot bypass validation, approval, or evidence.
- Every AI-suggested change is converted to explicit structured intent before execution.
- Risk scores are explainable and reviewable.

### Phase 8: Cost, Capacity, And Optimization

Goal:

Connect network and infrastructure changes to business impact, capacity, and cost.

Key deliverables:

- Capacity views:
  - device capacity
  - interface utilization
  - prefix/VLAN/VRF consumption
  - cloud network capacity
- Cost views:
  - cloud egress cost
  - circuit cost
  - underutilized links
  - stranded capacity
  - forecasted growth
- Optimization recommendations:
  - move traffic
  - resize capacity
  - retire unused resources
  - consolidate links or services
  - flag expensive architectures
- Change impact:
  - expected cost change
  - capacity impact
  - risk impact
  - service impact

Acceptance criteria:

- Engineers and leaders can see why a change matters operationally and financially.
- Optimization suggestions are backed by telemetry and source-of-truth data.

## Feature Matrix

| Feature Area | Current State | Roadmap Target |
| --- | --- | --- |
| Simple guided workflows | Add VLAN for Arista lab | Full workflow catalog across network services |
| Source of truth | Local YAML inventory and policy | NetBox/Nautobot, CMDB, IPAM, Git-backed intent |
| Jinja/config generation | Arista EOS VLAN template | Multi-vendor template registry and testing |
| Validation | Static policy checks | Policy-as-code, blast radius, dependency, CI checks |
| Lab proof | ORB Arista containerlab | Generated labs, pre-prod, digital twin validation |
| Device adapters | Arista EOS plus Rez bridge | Adapter SDK and multi-vendor capability matrix |
| Apply/rollback | Arista config session | Vendor-native commit/rollback semantics |
| Transparency | Live outcome panel and journal | Full audit timeline and signed evidence |
| Scale | Lab slice | 25k+ devices with distributed workers |
| Observability | Basic state bridge | Telemetry, drift, compliance, health views |
| AI/ML | Not yet | AI-assisted intake, risk, explanation, anomaly detection |
| Cost/capacity | Not yet | Capacity forecasts and optimization recommendations |

## Workflow Catalog Roadmap

Start with narrow, safe workflows. Do not add broad "run arbitrary config" features.

### Network Access

- Add VLAN
- Remove VLAN
- Rename VLAN
- Add SVI
- Update SVI description
- Add access port
- Update trunk allowed VLANs
- Move device to standard access profile

### Routing

- Add static route
- Add BGP neighbor
- Update prefix policy
- Add VRF
- Add route-target import/export

### Data Center Fabric

- Onboard tenant
- Add VRF/VNI
- Add server port
- Add MLAG pair intent
- Validate EVPN control-plane state

### Security And Segmentation

- Add ACL entry
- Update firewall object
- Update firewall rule
- Validate guest/PCI segmentation
- Validate management-plane protection

### Cloud And Hybrid

- Create VPC/VNet network intent
- Add transit attachment
- Add cloud firewall policy
- Validate hybrid route propagation
- Validate cloud/on-prem prefix ownership

## Production Architecture Target

The production architecture should have these components:

- UI for request, review, evidence, and operations.
- API gateway for workflow execution and integration.
- Workflow engine for state transitions.
- Policy engine for deterministic enforcement.
- Source-of-truth connector layer.
- Template and intent registry.
- Adapter SDK for vendor and platform operations.
- Worker queue for asynchronous execution.
- Artifact store for generated files and evidence.
- PostgreSQL for durable records.
- Secrets integration.
- Telemetry and state ingestion.
- Drift engine.
- Audit/reporting service.
- Notification and ticketing integrations.

## Adoption Roadmap

### Stage 1: Engineering Confidence

- Use the Arista lab for demos.
- Show click-by-click transparency.
- Teach the artifact chain without calling it training.
- Collect feedback from network engineers.

### Stage 2: Shadow Mode

- Run the platform against production source-of-truth data.
- Generate candidate changes but do not apply.
- Compare generated output to existing manual runbooks.
- Track false positives and missing validations.

### Stage 3: Low-Risk Production

- Start with read-only checks and low-risk changes.
- Require approval.
- Require rollback plan.
- Execute on limited device groups.

### Stage 4: Standard Change Factory

- Move repeatable changes into the platform.
- Reduce manual CLI.
- Track cycle time, failure rate, rollback rate, and drift reduction.

### Stage 5: Service-Level Automation

- Move from device changes to service outcomes.
- Integrate with cloud, data center, backbone, telemetry, and incident workflows.

## Metrics

Platform success should be measured by operational outcomes:

- Change lead time.
- Change failure rate.
- Mean time to rollback.
- Percent of changes with complete evidence.
- Percent of changes validated before execution.
- Percent of drift detected and remediated.
- Manual CLI reduction.
- Reusable workflow count.
- Device coverage by vendor and region.
- Engineer adoption rate.
- Audit finding reduction.

## Near-Term Next Steps

1. Harden the current Arista lab workflow into a clean demo-ready slice.
2. Add source-of-truth view in the UI.
3. Add a formal workflow state machine.
4. Add approval and role model stubs.
5. Add secrets abstraction.
6. Add one more workflow, preferably `remove_vlan` or `update_trunk_allowed_vlan`.
7. Add adapter SDK interfaces.
8. Add mocked multi-vendor adapter tests.
9. Add async job queue design.
10. Prepare a production-readiness checklist for leadership review.

