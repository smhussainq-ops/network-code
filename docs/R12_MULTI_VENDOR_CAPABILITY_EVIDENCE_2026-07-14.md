# R12 Multi-Vendor Capability Evidence - 2026-07-14

## Verdict

The product now publishes a machine-readable, fail-closed support contract for every registered read platform. Adapter registration no longer implies production write support. Selected non-Arista hardware and manager paths remain external acceptance gates.

## Product contract

`GET /api/platform/capabilities` returns `support_matrix` using only these reviewed states:

- `GA`
- `pilot-certified`
- `contract-tested`
- `read-only`
- `manager-assisted`
- `hardware-blocked`
- `planned`
- `unsupported`

Every registered platform receives an explicit status for discovery, SSH read, API read, Shell, configured state, Network Map, Network Health, Rez RCA, validation, dry-run, write, verify, rollback, and manager execution.

Unknown platforms and undeclared capabilities return `unsupported`. The UI may not infer write support from the presence of an adapter.

## Launch-relevant status

| Platform/path | Current evidence-bound status |
| --- | --- |
| Arista EOS | Pilot-certified for discovery, SSH, Shell, configured state, Map, Health, Rez RCA, validation, dry-run, write, verify, and rollback. eAPI read is contract-tested. |
| Fortinet FortiGate | Pilot-certified for discovery, SSH/API reads, configured state, Rez RCA, and validation. Network Map/Health are contract-tested. Direct write remains planned. |
| Cisco IOS/IOS-XE | Read and platform contracts are tested. Dry-run is offline validation plus generated diff; no native candidate-commit claim. Write remains planned. |
| Cisco NX-OS, ASA, Junos, SR Linux, Aruba AOS-CX, Cisco SD-WAN, Meraki, PAN-OS | Registered read paths are contract-tested only until selected hardware is proven. Unavailable features remain explicit. |
| FortiManager and Panorama | Manager contracts exist, but candidate/install/verify/rollback remain `hardware-blocked` until real controllers are tested. |

## Rez math safety boundary

Rez now has an explicit telemetry interpretation contract for every active collector. Unknown vendors and undeclared telemetry sections fail closed. Missing platform provenance is recorded as `unknown`; the normalizer no longer silently labels missing platforms as Arista EOS.

This prevents a parser or math engine from treating telemetry that was never collected as a healthy or failed condition. Synthetic scenarios must declare their reference platform explicitly.

## Git and Ansible

- Git-backed change history is implemented and the Arista end-to-end loop records plan, dry-run, approval, apply, verify, rollback, and evidence checkpoints.
- Guided Ansible generation and runner-local execution are implemented without requiring Python authoring. A live ORB read/check workflow completed against Arista EOS with runner-local credentials and no device write.
- Windows Ansible runtime/collection proof remains an R4 acceptance gate. A paid pilot must prove the exact collection and platform selected for that customer.

## Verification

```text
Netcode capability/API focused tests: 9 passed
Rez capability/proxy/firewall/hardware focused tests: 85 passed
Rez state-normalization and 90-scenario regression: 164 passed, 17 expected xfails
Netcode complete suite: 414 passed
Rez contract suite: 2,976 passed, 4 skipped, 31 expected xfails
Rez UI: production build passed; 135 tests passed
```

The UI production build warning for its current monolithic JavaScript chunk remains performance debt; it is not represented as a functional failure.

## External proof still required

- Prove each paid pilot's selected read/write platform on real hardware or its real manager/controller.
- Prove Windows Shell and Local Connector behavior for the Community path.
- Do not change `hardware-blocked`, `planned`, or `contract-tested` to `pilot-certified` based on a fixture, adapter registration, or marketing requirement.
