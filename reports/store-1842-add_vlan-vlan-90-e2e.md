# Netcode Change Report

Generated: 2026-07-01T04:09:43.152715+00:00

Verdict: PASS

## Intent YAML

```yaml
change_type: add_vlan
site: store-1842
targets:
  device_ids:
  - v2-store1
  device_group: access-switches
vlan:
  id: 90
  name: GUEST_WIFI
  subnet: 10.42.90.0/24
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

Template: `/tmp/netcode-platform-test/templates/arista/add_vlan.j2`

## Rendered Arista EOS Config

```eos
vlan 90
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
git checkout -b change/add_guest_vlan
git add intents/examples/add_guest_vlan.yaml
git commit -m "Add network intent add_guest_vlan"
```

## Current Git Diff

```diff
(No tracked diff yet. New files may be untracked.)
```

## End-To-End Phases

- PASS: Static Pipeline - YAML, Jinja rendering, Git evidence, and static validation completed.
- PASS: Adapter Contract - Execution adapter is registered and Rez state adapter supports this platform.
- PASS: Arista Lab Dry-Run - EOS accepted candidate config in a config session and the session was aborted.
- PASS: Arista Lab Apply And Verify - VLAN 90 with name GUEST_WIFI is present on the lab device.

## Arista Lab Evidence

```json
{
  "dry_run": {
    "status": "pass",
    "action": "dry-run",
    "device_id": "v2-store1",
    "message": "EOS accepted candidate config in a config session and the session was aborted.",
    "session_name": "netcode_1782878953",
    "evidence": {
      "diff": "show session-config diffs\n--- system:/running-config\n+++ session:/netcode_1782878953-session-config\n+vlan 90\n+   name GUEST_WIFI\nv2-store1(config-s-netcode_17-vlan-90)#",
      "transcript": [
        {
          "command": "configure session netcode_1782878953",
          "output": "configure session netcode_1782878953\nv2-store1(config-s-netcode_17)#"
        },
        {
          "command": "vlan 90",
          "output": "vlan 90\nv2-store1(config-s-netcode_17-vlan-90)#"
        },
        {
          "command": "   name GUEST_WIFI",
          "output": "   name GUEST_WIFI\nv2-store1(config-s-netcode_17-vlan-90)#"
        },
        {
          "command": "show session-config diffs",
          "output": "show session-config diffs\n--- system:/running-config\n+++ session:/netcode_1782878953-session-config\n+vlan 90\n+   name GUEST_WIFI\nv2-store1(config-s-netcode_17-vlan-90)#"
        },
        {
          "command": "abort",
          "output": "abort\nv2-store1#"
        }
      ]
    }
  },
  "apply": {
    "status": "pass",
    "action": "apply",
    "device_id": "v2-store1",
    "message": "VLAN 90 with name GUEST_WIFI is present on the lab device.",
    "session_name": "netcode_1782878967",
    "evidence": {
      "session": {
        "diff": "show session-config diffs\n--- system:/running-config\n+++ session:/netcode_1782878967-session-config\n+vlan 90\n+   name GUEST_WIFI\nv2-store1(config-s-netcode_17-vlan-90)#",
        "transcript": [
          {
            "command": "configure session netcode_1782878967",
            "output": "configure session netcode_1782878967\nv2-store1(config-s-netcode_17)#"
          },
          {
            "command": "vlan 90",
            "output": "vlan 90\nv2-store1(config-s-netcode_17-vlan-90)#"
          },
          {
            "command": "   name GUEST_WIFI",
            "output": "   name GUEST_WIFI\nv2-store1(config-s-netcode_17-vlan-90)#"
          },
          {
            "command": "show session-config diffs",
            "output": "show session-config diffs\n--- system:/running-config\n+++ session:/netcode_1782878967-session-config\n+vlan 90\n+   name GUEST_WIFI\nv2-store1(config-s-netcode_17-vlan-90)#"
          },
          {
            "command": "commit",
            "output": "commit\nv2-store1#"
          }
        ]
      },
      "verification": {
        "commands": {
          "show vlan id 90": "show vlan id 90\nVLAN  Name                             Status    Ports\n----- -------------------------------- --------- -------------------------------\n90    GUEST_WIFI                       active    \n\nv2-store1#",
          "show running-config | section ^vlan 90": "show running-config | section ^vlan 90\nvlan 90\n   name GUEST_WIFI\nv2-store1#"
        }
      }
    }
  }
}
```
