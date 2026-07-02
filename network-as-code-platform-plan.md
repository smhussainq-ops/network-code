# Network-As-Code Training Platform Plan

## Vision

Build a world-class network-as-code platform that lets engineers perform safe network changes through simple guided workflows while quietly teaching them Git, YAML, Jinja, validation, Arista EOS configuration, and automation discipline.

The platform should not feel like a classroom. It should feel like the normal way to make network changes.

The core idea:

> Engineers start with simple wizards, but every step exposes the artifact it creates: intent YAML, Jinja templates, rendered config, Git diffs, validation results, lab evidence, and deployment reports.

This creates a glass-box system where engineers learn by doing real work.

## Product Principles

1. **Simple Entry, Deep Visibility**
   - The default experience is a guided wizard.
   - Every generated artifact can be expanded and inspected.
   - Nothing the platform does is hidden.

2. **Training Through Repetition**
   - Engineers repeatedly see how form choices become YAML, how YAML feeds Jinja, how Jinja renders EOS config, and how Git tracks the change.
   - The platform teaches without announcing that it is teaching.

3. **Validation First**
   - The platform is primarily a validation engine.
   - Deployment is allowed only after the change has passed required gates.
   - Any failed, missing, or uncertain validation blocks the change.

4. **Fail Closed**
   - If the validator crashes, the device is unreachable, the lab is unavailable, or evidence is incomplete, the verdict is failure.
   - No change proceeds on assumptions.

5. **Glass-Box Reporting**
   - Every run produces a clear report showing:
     - user intent
     - generated YAML
     - Jinja template used
     - rendered Arista EOS config
     - Git branch and diff
     - validation checks
     - Arista lab dry-run output
     - final pass/fail verdict

6. **Lab Before Production**
   - The first target is the Arista containerlab environment running on the ORB VM.
   - The lab must prove the platform loop before production rollout is designed.

## Target User Experience

Example workflow: **Add Guest VLAN**

### Step 1: Wizard

The platform asks simple network-focused questions:

```text
Store: 1842
Device group: access-switches
VLAN ID: 90
VLAN name: GUEST_WIFI
Subnet: 10.42.90.0/24
Purpose: guest
Should this VLAN reach POS/PCI networks? No
```

### Step 2: Generated YAML Intent

The platform creates an intent file:

```yaml
change_type: add_vlan
site: store-1842
targets:
  device_group: access-switches
vlan:
  id: 90
  name: GUEST_WIFI
  subnet: 10.42.90.0/24
  purpose: guest
policy:
  pci_reachable: false
```

The engineer can expand this section and learn that YAML is the structured intent format.

### Step 3: Jinja Template Preview

The platform shows the template used to generate config:

```jinja2
vlan {{ vlan.id }}
   name {{ vlan.name }}
```

The engineer sees how variables from YAML become device configuration.

### Step 4: Rendered Arista EOS Config

The platform renders the final candidate config:

```eos
vlan 90
   name GUEST_WIFI
```

The engineer learns EOS syntax by repeatedly seeing generated CLI next to the original intent.

### Step 5: Git Workflow

The platform handles Git, but explains what it did:

```text
Created branch: change/store-1842-add-vlan-90
Created intent file: intents/store-1842/add-vlan-90.yaml
Created commit: Add guest VLAN 90 to store 1842
```

Expandable command view:

```bash
git checkout -b change/store-1842-add-vlan-90
git add intents/store-1842/add-vlan-90.yaml
git commit -m "Add guest VLAN 90 to store 1842"
```

### Step 6: Validation

The platform runs deterministic checks:

```text
PASS: VLAN ID 90 is in the allowed range.
PASS: VLAN name matches naming standards.
PASS: Subnet 10.42.90.0/24 does not overlap existing store networks.
PASS: Guest VLAN is not allowed to reach PCI/POS networks.
PASS: Rendered Arista config is deterministic.
PASS: Candidate change does not modify management access.
```

Failures must explain what happened, why it matters, and how to fix it.

Example:

```text
FAIL: Guest VLAN would be allowed to reach PCI VLAN 30.

Reason:
The requested policy allows guest traffic to reach 10.42.30.0/24.
That violates the PCI segmentation invariant.

Suggested fix:
Set policy.pci_reachable to false and apply the standard guest ACL template.
```

### Step 7: Arista Lab Dry-Run

The platform connects to the ORB VM containerlab Arista node and uses EOS-safe mechanisms:

```text
PASS: Connected to clab node ceos-access1.
PASS: Created EOS config session.
PASS: Loaded candidate config into session.
PASS: EOS accepted the candidate config.
PASS: Session diff matches the expected VLAN-only change.
PASS: Aborted dry-run session without applying config.
```

### Step 8: Arista Lab Apply And Verify

Only after validation and dry-run pass:

```text
PASS: Applied candidate config to lab node.
PASS: VLAN 90 exists in running config.
PASS: VLAN 90 is active.
PASS: Management access remained healthy.
PASS: No unexpected config drift detected.
```

### Step 9: Report

The platform writes a full report that can be reviewed later:

```text
Verdict: PASS
Change: Add guest VLAN 90 to store-1842
Branch: change/store-1842-add-vlan-90
Commit: <commit-id>
Template: templates/arista/add_vlan.j2
Lab target: clab arista access switch
Evidence: validation output, EOS diff, post-check commands
```

## Platform Architecture

### 1. CLI / Wizard Layer

Initial interface:

```bash
netcode init
netcode wizard add-vlan
netcode render
netcode validate
netcode lab dry-run
netcode lab apply
netcode report
```

The CLI should feel like a guided assistant, not a pile of raw automation commands.

Future UI work should call the same backend APIs.

### 2. Intent Model

Defines what engineers are allowed to ask for in structured form.

Initial intent type:

- `add_vlan`

Future intent types:

- update VLAN
- remove VLAN
- add SVI
- update ACL
- add interface description
- configure access port
- configure trunk allowed VLAN
- add static route
- update BGP neighbor

### 3. Inventory / Source Of Truth

Stores:

- sites
- devices
- device roles
- vendors
- management IPs
- environment labels
- VLAN reservations
- subnets
- archetypes
- risk tiers

Initial version can be YAML files in the repo.

Future versions can integrate with Nautobot, NetBox, ServiceNow, or an internal source of truth.

### 4. Template / Rendering Engine

Uses Jinja to convert intent into vendor-specific config.

Initial vendor:

- Arista EOS

Required behavior:

- deterministic rendering
- no hidden side effects
- clear variable mapping
- template path recorded in report
- rendered output saved as evidence

### 5. Validation Engine

The heart of the platform.

Validation layers:

1. schema validation
2. intent linting
3. inventory consistency
4. template render validation
5. static policy checks
6. blast-radius checks
7. Arista lab dry-run
8. Arista lab post-change verification

All validation results must be structured and explainable.

### 6. Policy / Invariant Catalog

Version-controlled rules that define what must always be true.

Initial invariants:

- VLAN IDs must be in approved ranges.
- VLAN names must follow standards.
- Subnets must not overlap.
- Guest networks must not reach PCI/POS networks.
- Management access must not be modified by low-risk changes.
- Rendered config must only touch the intended feature area.
- Unknown validation result equals failure.

Future invariants:

- default route must remain present
- WAN backup path must remain present
- no unsafe `permit ip any any` in protected zones
- no changes outside approved maintenance windows
- no unauthorized device roles targeted
- routing neighbors must remain established

### 7. Arista Device Adapter

Responsible for safe interaction with EOS.

Capabilities:

- connect to EOS lab nodes
- collect running config
- create config session
- load candidate config
- show session diffs
- abort session
- commit session when explicitly allowed
- run post-change show commands
- return structured evidence

The adapter should isolate vendor/device mechanics from the rest of the platform.

### 8. Lab Harness

Targets the ORB VM containerlab Arista lab.

Responsibilities:

- discover or load lab nodes
- verify lab reachability
- run dry-runs
- apply approved lab changes
- collect post-change evidence
- optionally reset lab state between tests

The first lab target should be a simple Arista cEOS topology.

### 9. Change Orchestrator

Tracks state transitions:

```text
draft
rendered
static_validated
lab_dry_run_passed
lab_applied
lab_verified
ready_for_review
```

Production states can be added later:

```text
approved
canary_scheduled
canary_deployed
canary_verified
ring_deployed
fleet_complete
rolled_back
```

### 10. Reporting Layer

Creates human-readable and machine-readable reports.

Formats:

- Markdown for engineers
- JSON for automation

Report must include:

- intent
- generated YAML
- rendered config
- Git branch/diff/commit
- validation checks
- dry-run evidence
- apply evidence
- post-check evidence
- final verdict

## Repository Structure

Proposed starting layout:

```text
network-as-code-platform/
  README.md
  pyproject.toml
  netcode/
    cli.py
    intent/
    inventory/
    rendering/
    validation/
    policy/
    devices/
    lab/
    reports/
    orchestration/
  inventories/
    lab.yaml
    sites.yaml
  intents/
    examples/
      add_guest_vlan.yaml
  templates/
    arista/
      add_vlan.j2
  policies/
    invariants.yaml
  reports/
  tests/
    unit/
    integration/
    lab/
```

## Implementation Phases

### Phase 1: Product Foundation

Goal:

Create the project scaffold and define the core contracts.

Deliverables:

- Python package structure
- CLI entrypoint
- repo initialization command
- sample inventory
- sample policy catalog
- sample add-VLAN intent
- test framework
- basic documentation

Success criteria:

- `netcode init` creates a usable workspace.
- Tests run locally.
- The repo structure is understandable to a network engineer.

### Phase 2: Guided Wizard

Goal:

Create the first simple workflow that generates valid intent YAML.

Deliverables:

- `netcode wizard add-vlan`
- guided prompts
- generated YAML file
- plain-English explanation of generated fields
- validation of required inputs

Success criteria:

- An engineer can create an add-VLAN intent without knowing YAML.
- The engineer can expand and inspect the YAML afterward.

### Phase 3: Rendering

Goal:

Convert intent into Arista EOS config using Jinja.

Deliverables:

- Arista add-VLAN template
- `netcode render`
- rendered config artifact
- template-variable trace
- renderer unit tests

Success criteria:

- Same input always renders same output.
- Rendered config clearly maps back to intent fields.

### Phase 4: Static Validation

Goal:

Prove the candidate is safe before touching the lab.

Deliverables:

- schema validation
- VLAN policy checks
- subnet overlap checks
- PCI/guest segmentation checks
- blast-radius checks
- explainable validation result model

Success criteria:

- Unsafe changes fail with clear reasons.
- Validator errors fail closed.
- All rules have unit tests.

### Phase 5: Git Training Flow

Goal:

Make Git part of the normal workflow while hiding unnecessary complexity.

Deliverables:

- create change branch
- show Git diff
- commit generated intent
- explain the Git commands used
- prevent committing failed validation artifacts as approved changes

Success criteria:

- Engineer learns branch, diff, and commit through normal platform use.
- Every change is traceable.

### Phase 6: Arista Lab Dry-Run

Goal:

Connect to the ORB VM containerlab Arista environment and dry-run candidate config safely.

Deliverables:

- Arista lab inventory
- EOS connection adapter
- config session support
- session diff collection
- abort dry-run session
- structured evidence capture

Success criteria:

- Platform can prove EOS accepts candidate config without applying it.
- Diff matches expected change scope.

### Phase 7: Arista Lab Apply And Verify

Goal:

Apply validated config to the lab and verify actual device state.

Deliverables:

- explicit lab apply command
- post-change show commands
- VLAN existence verification
- management reachability check
- unexpected drift detection
- rollback/reset strategy for lab

Success criteria:

- Lab apply only runs after validation and dry-run pass.
- Verification proves the intended state exists.
- Failure produces a useful report.

### Phase 8: Glass-Box Report

Goal:

Produce a complete learning and audit artifact.

Deliverables:

- Markdown report
- JSON report
- intent summary
- generated YAML
- Jinja template reference
- rendered config
- Git diff
- validation result
- lab dry-run evidence
- lab verification evidence
- final verdict

Success criteria:

- A senior engineer can audit the change.
- A junior engineer can learn from the change.
- The report is useful without re-running the tool.

### Phase 9: Expand Change Types

Goal:

Add more common network workflows after the add-VLAN path is excellent.

Candidate workflows:

- add access port
- update trunk allowed VLAN
- create SVI
- add ACL rule
- add static route
- update BGP neighbor description

Success criteria:

- New workflows reuse the same platform contracts.
- No new workflow bypasses validation or reporting.

### Phase 10: Production Promotion Design

Goal:

Only after the lab loop is reliable, design production rollout.

Required capabilities:

- risk tiering
- approval workflow
- change windows
- canary deployment
- commit-confirmed or rollback timer equivalent
- health-gated ring rollout
- rollback evidence
- immutable audit trail

Success criteria:

- Production rollout is a thin layer on top of proven validation, reporting, and device adapters.

## First Milestone

The first milestone should be:

> An engineer runs a guided wizard to add a guest VLAN, the platform generates YAML, renders Arista EOS config through Jinja, creates a Git branch and diff, validates the change, dry-runs it against the ORB VM containerlab Arista lab, applies it to the lab, verifies the result, and writes a complete glass-box report.

This proves the core loop:

```text
wizard -> YAML -> Jinja -> EOS config -> Git diff -> validation -> lab dry-run -> lab apply -> verification -> report
```

## What We Should Not Build First

Do not start with:

- full web UI
- fleet-wide production deployment
- complex approval workflows
- multi-vendor abstraction
- Batfish integration
- ServiceNow integration
- NetBox/Nautobot integration

Those can come later. The first version must make one workflow flawless.

## Definition Of Done For The First Workflow

The add-VLAN workflow is complete when:

- wizard input creates valid YAML
- YAML can be read and edited by a human
- Jinja template renders deterministic EOS config
- platform shows the rendered config before lab execution
- validation blocks unsafe input
- validation failures explain what to fix
- Git branch and diff are created
- Arista lab dry-run succeeds using config sessions
- lab apply requires explicit confirmation
- post-checks prove VLAN state
- report captures the entire chain
- unit and lab tests pass

## Long-Term Direction

The platform should eventually become:

- a safe network change system
- a network automation training environment
- a validation engine
- an audit system
- a living runbook
- a bridge from traditional CLI operations to network-as-code engineering

The strongest outcome is that engineers do not feel like they are taking a course. They simply use the platform to make changes, and over time they become fluent in Git, YAML, Jinja, validation, and network-as-code practices.
