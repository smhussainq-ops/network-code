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
- Added `GET /api/desired-state/catalog`.
- Added `POST /api/desired-state/plan`.
- Added `POST /api/verify/intent`.
- Added `GET /api/audit/sessions`.
- Added `git_workspace_status()` helper.
- Added typed desired-state models for:
  - VLAN
  - Interface config
  - BGP neighbor
  - ACL rule
  - Site/device source-of-truth intent
- Added per-intent Jinja templates and validation gates.
- Added per-intent plan metadata for risk, lab apply support, and production
  lock state.
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
  - Drift
  - Evidence
- Rebuilt Desired State as a dynamic intent builder instead of a fixed VLAN
  form.
- Added change-type cards for VLAN, interface, BGP, ACL, and site/device intent.
- Added dynamic form fields per selected change type.
- Added apply-gate visibility so unsupported write paths are locked in the UI.
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
- Added drift view.
- Added evidence tabs for YAML, generated commands, validation, lab proof, Git,
  audit sessions, and jobs.

## Safety Behavior

- Setup does not touch device config.
- Discovery does not touch device config.
- Plan does not touch device config.
- Static validation does not touch device config.
- Dry-run uses EOS config session and aborts it.
- Apply is locked until validation and dry-run pass.
- Apply is also locked when an intent type does not have lab write support.
- Production write remains locked for every current intent type.
- Verification is read-only.
- Rollback is available after apply.
- Every dry-run/apply/rollback command session is stored in job evidence and
  exposed through the Audit evidence tab.

## Tests

- Updated UI route test to assert the new MVP flow.
- Added desired-state catalog and multi-intent plan tests.
- Added audit session transcript test.
- Existing backend tests remain in place.

## Out of Scope

- Multi-vendor config push.
- Production RBAC and approvals.
- NetBox/Nautobot write integration.
- Enterprise secrets management.
- Change-window enforcement.
