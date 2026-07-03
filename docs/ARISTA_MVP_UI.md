# Netcode Arista MVP UI

Date: 2026-07-03

This MVP is the first clean product slice of Netcode as a Terraform-style
network-as-code platform.

## Goal

Let a network engineer use the UI to:

1. Check workspace readiness.
2. Edit platform settings from the UI.
3. Discover the Arista lab switch.
4. Save the device into source of truth.
5. Define desired network state from multiple intent types.
6. Create a plan.
7. Review validation.
8. Dry-run the candidate in an EOS config session.
9. Apply only after dry-run proof.
10. Verify live state.
11. Detect drift.
12. Review evidence and audit sessions.

## MVP Scope

The default configuration is the Arista lab slice:

- Site: `store-1842`
- Device: `v2-store1`
- Vendor: Arista EOS
- Lab IP: `172.100.1.41`
- Change: add VLAN `90`
- VLAN name: `GUEST_WIFI`
- Subnet: `10.42.90.0/24`

Supported desired-state plan/validate types:

- Add VLAN
- Interface config
- BGP neighbor
- ACL rule
- Site/device source-of-truth intent

Arista lab dry-run/apply/rollback gates are exposed per intent type. Site/device
intent is source-of-truth only and keeps device writes locked.

The defaults are editable from the Setup screen and persisted in
`.netcode/ui_config.yaml`. The UI uses that configuration for:

- Git repo URL, branch, commit message, and artifact globs.
- Source-of-truth provider, inventory path, policy path, and template directory.
- Credential profile, username, and default SSH port.
- Discovery host, vendor, device name, site, groups, and port.
- Desired-state common defaults.
- Desired-state cards, field labels, defaults, select choices, and write gates.
- Workflow controls such as dry-run requirement, production lock, canary size,
  and batch size.
- Audit settings and config change history.

The MVP uses:

- Git workspace status from the local repo.
- Configured local YAML source of truth, defaulting to `inventories/lab.yaml`.
- Rez read adapters for discovery and state collection.
- Jinja template rendering from the configured template directory.
- Static validation from the configured policy file.
- Arista EOS config sessions for dry-run, apply, rollback, and verification.
- SQLite job/change records under `.netcode/netcode.db`.
- Audit session extraction from durable job transcripts.

## UI Flow

### Home

Shows the simple product entry points:

- Set up platform
- Discover devices
- Create network change
- Review evidence

### Setup

Checks:

- Git repo status
- Source-of-truth health
- Rez adapter registry
- Arista lab reachability

Also exposes editable platform configuration:

- Quick controls for Git, source of truth, credentials, discovery defaults,
  desired-state defaults, and workflow gates.
- Full JSON editor for every option the UI consumes.
- Save, reload, and reset actions.

Device config writes: none.

### Inventory

Runs Rez discovery against the lab device. Discovery is read-only.

The UI shows:

- Detected platform
- Adapter used
- Hostname
- State summary
- Source-of-truth candidate

The engineer can then save the reviewed device to local YAML source of truth.
Passwords entered for discovery are not written to source of truth.

### Desired State

The engineer first chooses the network outcome:

- Add VLAN
- Interface config
- BGP neighbor
- ACL rule
- Site/device intent

The form then changes to show only fields relevant to that intent.

The UI generates:

- Intent YAML
- Rendered Arista EOS candidate config where the intent has device commands
- Static validation report
- Git review plan
- Apply gate metadata

Device config writes: none.

### Plan

Shows the planned change before device contact:

- Target device
- VLAN action
- Risk summary
- Exact generated commands
- Terraform-style change summary

Device config writes: none.

### Validate

Shows:

- Policy checks
- Config scope checks
- Git review plan
- Lab dry-run result after dry-run is executed

Dry-run sends candidate commands into an EOS config session and aborts the
session. It does not commit the change.

### Apply

Apply remains locked until:

- Plan exists
- Validation passed
- Lab dry-run passed
- Selected intent type supports Arista lab writes

After apply, the UI allows:

- Rez live-state verification
- Rollback

### Drift

Drift checks are read-only.

For VLAN intent, the UI compares desired VLAN state against live Rez state.
For non-VLAN intent, the UI collects live state and records that deep drift
comparison still needs the next typed verifier.

### Evidence

Shows:

- Overview
- Intent YAML
- Generated commands
- Validation
- Lab proof
- Git review plan
- UI configuration and configuration history
- Jobs
- Audit sessions with command transcripts

## Current Limits

This MVP is intentionally honest about scope:

- Multi-vendor read/discovery is available through Rez.
- Multi-vendor config push is not complete.
- Arista EOS is the only wired write/apply path.
- VLAN has the strongest current end-to-end verification through Rez.
- Non-VLAN Arista intents have plan/validate and lab command-session plumbing,
  but production rollout remains locked.
- NetBox/Nautobot are not active source-of-truth providers yet.
- Approval/RBAC is out of scope for this single-user MVP.
- Production change windows and enterprise credential handling are not complete.

## Success Criteria

The UI is successful when an engineer can say:

> I defined the desired state, saw the exact plan, validated it, dry-ran it,
> applied it only after proof, verified live state, and have evidence for review.

For any device write, the success criteria also require:

> Every command session is recorded as a durable job and visible from the Audit
> evidence view.
