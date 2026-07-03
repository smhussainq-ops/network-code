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
- Added editable UI configuration APIs:
  - `GET /api/config/ui`
  - `POST /api/config/ui`
  - `POST /api/config/ui/reset`
  - `GET /api/config/ui/history`
- Added `.netcode/ui_config.yaml` for persisted platform/UI settings.
- Added `.netcode/ui_config_history.yaml` for configuration audit history.
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
- Removed hardcoded visible lab defaults from the browser forms; defaults now
  load from the editable platform configuration.
- Added Setup controls for:
  - Git repo URL, branch, and commit message
  - Source-of-truth provider and paths
  - Credential profile, username, and port
  - Discovery defaults
  - Desired-state defaults
  - Workflow gates, canary size, and batch size
- Added full configuration JSON editor so every UI-consumed option can be
  changed without code edits.
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
  UI configuration, audit sessions, and jobs.

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
- Every UI configuration save/reset is written to config history and exposed
  through the Config evidence tab.

## Tests

- Updated UI route test to assert the new MVP flow.
- Added desired-state catalog and multi-intent plan tests.
- Added audit session transcript test.
- Added UI configuration persistence test.
- Added configured source-of-truth path test.
- Existing backend tests remain in place.

## Out of Scope

- Multi-vendor config push.
- Production RBAC and approvals.
- NetBox/Nautobot write integration.
- Enterprise secrets management.
- Change-window enforcement.
