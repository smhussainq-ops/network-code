# Netcode Arista MVP UI

Date: 2026-07-03

This MVP is the first clean product slice of Netcode as a Terraform-style
network-as-code platform.

## Goal

Let a network engineer use the UI to:

1. Check workspace readiness.
2. Discover the Arista lab switch.
3. Save the device into source of truth.
4. Define desired network state.
5. Create a plan.
6. Review validation.
7. Dry-run the candidate in an EOS config session.
8. Apply only after dry-run proof.
9. Verify live state.
10. Review evidence.

## MVP Scope

Supported end-to-end change:

- Site: `store-1842`
- Device: `v2-store1`
- Vendor: Arista EOS
- Lab IP: `172.100.1.41`
- Change: add VLAN `90`
- VLAN name: `GUEST_WIFI`
- Subnet: `10.42.90.0/24`

The MVP uses:

- Git workspace status from the local repo.
- Local YAML source of truth in `inventories/lab.yaml`.
- Rez read adapters for discovery and state collection.
- Jinja template rendering from `templates/arista/add_vlan.j2`.
- Static validation from `policies/invariants.yaml`.
- Arista EOS config sessions for dry-run, apply, rollback, and verification.
- SQLite job/change records under `.netcode/netcode.db`.

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

The engineer defines the network outcome in a form.

The UI generates:

- Intent YAML
- Rendered Arista EOS candidate config
- Static validation report
- Git review plan

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

After apply, the UI allows:

- Rez live-state verification
- Rollback

### Evidence

Shows:

- Overview
- Intent YAML
- Generated commands
- Validation
- Lab proof
- Git review plan
- Jobs

## Current Limits

This MVP is intentionally honest about scope:

- Multi-vendor read/discovery is available through Rez.
- Multi-vendor config push is not complete.
- Arista EOS is the only wired write/apply path.
- NetBox/Nautobot are not active source-of-truth providers yet.
- Approval/RBAC is out of scope for this single-user MVP.
- Production change windows and enterprise credential handling are not complete.

## Success Criteria

The UI is successful when an engineer can say:

> I defined the desired state, saw the exact plan, validated it, dry-ran it,
> applied it only after proof, verified live state, and have evidence for review.
