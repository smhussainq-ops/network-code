# Change Log: Arista MVP UI Rebuild

Date: 2026-07-03

## Summary

Rebuilt the UI around a clear network-as-code product flow:

```text
Home -> Setup -> Inventory -> Desired State -> Plan -> Validate -> Apply -> Evidence
```

This replaces the previous button-heavy interface with a focused MVP experience
for the Arista lab and the local Git repo.

## Backend Changes

- Added `GET /api/git/status`.
- Added `git_workspace_status()` helper.
- Kept existing working APIs for:
  - Source of truth
  - Rez discovery
  - GitOps plan
  - Add VLAN intent
  - Static pipeline
  - Lab dry-run
  - Lab apply
  - Lab rollback
  - Live VLAN verification
  - Jobs and workflow evidence

## UI Changes

- Replaced the old console/journey split with one MVP product shell.
- Added left navigation:
  - Home
  - Setup
  - Inventory
  - Desired State
  - Plan
  - Validate
  - Apply
  - Evidence
- Added persistent outcome panel showing:
  - Expected result
  - Actual result
  - Artifact created or inspected
  - Whether device config changed
  - Next safe action
- Added setup health view for:
  - Git
  - Source of truth
  - Rez adapters
  - Arista lab
- Added inventory discovery flow using Rez.
- Added source-of-truth import from the UI.
- Added desired-state form for the Arista VLAN MVP.
- Added Terraform-style plan summary.
- Added validation gate view.
- Added apply/verify/rollback gate view.
- Added evidence tabs for YAML, generated commands, validation, lab proof, Git,
  and jobs.

## Safety Behavior

- Setup does not touch device config.
- Discovery does not touch device config.
- Plan does not touch device config.
- Static validation does not touch device config.
- Dry-run uses EOS config session and aborts it.
- Apply is locked until validation and dry-run pass.
- Verification is read-only.
- Rollback is available after apply.

## Tests

- Updated UI route test to assert the new MVP flow.
- Existing backend tests remain in place.

## Out of Scope

- Multi-vendor config push.
- Production RBAC and approvals.
- NetBox/Nautobot write integration.
- Enterprise secrets management.
- Change-window enforcement.
