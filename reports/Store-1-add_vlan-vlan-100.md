# Netcode Change Report

Generated: 2026-07-02T15:03:14.088279+00:00

Verdict: PASS

## Intent YAML

```yaml
change_type: add_vlan
site: Store-1
targets:
  device_ids:
  - v2-store1
  device_group: access-switches
vlan:
  id: 100
  name: GUEST_WIFI
  subnet: 10.1.1.0/24
  purpose: guest
  svi:
    enabled: false
policy:
  pci_reachable: false
  internet_reachable: true
metadata:
  requested_by: lab-engineer
  learning_mode: true
```

## Jinja Template

Template: `/Users/syedhussain/Documents/Network Automation/templates/arista/add_vlan.j2`

## Rendered Arista EOS Config

```eos
vlan 100
   name GUEST_WIFI
```

## Validation

- PASS: Intent Schema - Intent loaded into the add_vlan model.
- PASS: Target Resolution - All requested target devices resolve in inventory.
- PASS: VLAN Policy - VLAN ID and name match policy.
- PASS: Subnet Overlap - Requested subnet does not overlap known site subnets.
- PASS: PCI Segmentation - Segmentation policy is preserved for this intent.
- PASS: Rendered Config Scope - Rendered config only touches the intended VLAN feature scope.
- PASS: Deterministic Render - Same intent renders to the same EOS config every time.

## Git Teaching View

```bash
git checkout -b change/Store-1-add-vlan-100
git add intents/Store-1/Store-1-add-vlan-100.yaml
git commit -m "Add network intent Store-1-add-vlan-100"
```

## Current Git Diff

```diff
(No tracked diff yet. New files may be untracked.)
```
