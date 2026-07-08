# Netcode + Rez Full-Loop Certification — 2026-07-08

## Verdict

Slices 9-11 are implemented and committed. Slice 12 live certification passed for the core automation loop on the ORB Arista lab through the Netcode control plane and local runner:

`plan -> dry-run -> second-person approval -> apply -> verify -> rollback -> evidence record`

The UI must present this as a real workflow driven by live Netcode records, not as static marketing content.

## Repos And Commits

- Netcode repo: `/Users/syedhussain/Documents/Network Automation`
- Netcode branch: `main`
- Netcode commits in this certification set:
  - `9f49a84 api: queue Ansible packs on runner`
  - `c1896fe api: package Windows local runner`
  - `4c90bd4 deploy: add AWS pilot Netcode artifacts`
- Rez repo: `/Users/syedhussain/Dev/Claude/resonance-core`
- Rez branch: `codex/phase7-agent-sdk-migration-v2`
- Rez commits in this certification set:
  - `52586c1 ui: add runner-only Ansible pack workflow`

## Live Environment

- Netcode control plane: `http://127.0.0.1:8095`
- Execution mode: `NETCODE_EXECUTION=runner`
- Runner pool: `store-lab`
- Runner location: ORB VM `clab`
- Runner process: `python3 -u -m netcode.runner_agent run`
- Rez backend: `http://127.0.0.1:9005`
- Rez split mode env used:
  - `REZ_RUNNER_SPLIT_MODE=true`
  - `REZ_RUNNER_CONTROL_PLANE_URL=http://127.0.0.1:8095`
  - `REZ_RUNNER_BRIDGE_TOKEN=rez-bridge-proof-8095`
  - `REZ_V2_MATH_TOOLS=true`

## Runner Readiness Proof

Command:

```bash
curl -sS -X POST http://127.0.0.1:8095/api/readiness/devices \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Result:

- `ok: true`
- `tested: 26`
- `readable: 25`
- Failed device: `ssh-test`
- Failure reason: `Rez has no driver for platform linux`

Interpretation:

- The ORB runner is reaching the Arista lab devices.
- The one failure is an unsupported linux test entry, not an Arista automation failure.
- The UI should show this as a readiness warning, not hide it or claim 100% readiness.

## Slice 12 Live Automation Proof

### 1. Plan

Request:

```json
{
  "change_type": "add_vlan",
  "site": "store-1842",
  "device_id": "v2-store1",
  "requested_by": "slice12-requester",
  "values": {
    "vlan_id": 3998,
    "name": "SLICE12_CERT",
    "subnet": "10.42.98.0/24",
    "purpose": "slice12-cert",
    "pci_reachable": false
  }
}
```

Result:

- Change ID: `5bd718b4-35e4-4956-b6bd-537a106923e1`
- Intent path: `/Users/syedhussain/Documents/Network Automation/intents/store-1842/store-1842-add-vlan-3998.yaml`
- Workflow state: `validated`
- Plan title: `Add VLAN 3998 (SLICE12_CERT)`
- Risk: `Low for lab`
- Lab write supported: `true`
- Production write supported: `false`

### 2. Dry-Run / Canary Proof

Runner job: `7248b0ee-4f3a-4457-ae7b-deaa8b2e5495`

Result:

- Status: `completed`
- Action: `lab_dry-run`
- Message: `EOS accepted candidate config in a config session and the session was aborted.`

Key evidence:

```text
configure session netcode_1783475519
vlan 3998
   name SLICE12_CERT
show session-config diffs
abort
```

Interpretation:

- The runner opened an EOS config session.
- Candidate config was accepted.
- The config session was aborted, so dry-run did not write.

### 3. Human Approval Gate

Request:

```json
{
  "approved_by": "slice12-approver"
}
```

Result:

- Workflow state: `approved`
- Message: `Approved by slice12-approver. Apply is now unlocked.`

Interpretation:

- Apply was locked until dry-run passed and a second engineer approved.
- This is the human write boundary.

### 4. Apply

Runner job: `7f1e93aa-931c-4b59-a2d6-8b1559be009a`

Result:

- Status: `completed`
- Action: `lab_apply`
- Message: `VLAN 3998 with name SLICE12_CERT is present on the lab device.`

Key evidence:

```text
configure session netcode_1783475541
vlan 3998
   name SLICE12_CERT
show session-config diffs
commit
```

Interpretation:

- The change was applied by the runner, not by the SaaS/control-plane process.
- The change touched exactly the scoped VLAN config.

### 5. Verify Actual State

Result:

- `ok: true`
- Verification status: `pass`
- Device: `v2-store1`
- Message: `VLAN 3998 with name SLICE12_CERT is present on the lab device.`

Live verification commands:

```text
show vlan id 3998
show running-config | section ^vlan 3998
```

Observed output included:

```text
3998  SLICE12_CERT                     active
vlan 3998
   name SLICE12_CERT
```

### 6. Rollback

Runner job: `06bbc6c3-e157-47f3-a061-7ec0a87d94f9`

Result:

- Status: `completed`
- Action: `lab_rollback`
- Message: `VLAN 3998 is absent from the lab device.`

Interpretation:

- The test cleaned up the lab.
- The rollback path is runner-executed and evidence-backed.

### 7. Evidence Record

Endpoint:

```bash
GET /api/change/5bd718b4-35e4-4956-b6bd-537a106923e1/record
```

Record shape:

- `workflow_state: rolled_back`
- `plan.commands: vlan 3998 ...`
- `plan.rollback.commands: no vlan 3998`
- `safety.status: pass`
- `lab_proof.present: true`
- `apply_proof.present: true`
- `verify_proof.present: true`
- `rollback_record.present: true`
- `events: 8`

The Rez UI should consume this record directly.

## Slice 9 Ansible Status

Implemented:

- Netcode endpoint: `POST /api/workflow-packs/ansible/run`
- Rez proxy: `POST /api/netcode/workflow-packs/ansible/run`
- Runner action: `ansible_pack`
- Runner-local inventory and credentials only.
- No shell string for `ansible-playbook`; command is built as an argument list.
- Explicit target IDs are required.
- Canary/apply require an approved Netcode change.

Not live-certified:

- `ansible-playbook` availability on the ORB runner was not certified in this run.
- Do not claim live Ansible execution until the Windows/ORB runner has Ansible installed and a real playbook is executed.

## Slice 10 Windows Runner Status

Implemented:

- `GET /api/runner/download/windows/manifest`
- `GET /api/runner/download/windows`
- Generated ZIP includes:
  - `README.md`
  - `install-runner.ps1`
  - `start-runner.ps1`
  - `import-inventory.ps1`
  - `sample-inventory.yaml`
  - `netcode-shell-profile.json`

Security posture:

- Package contains no real secrets.
- Runner uses outbound control-plane connection.
- Device credentials remain local on the runner.

Not yet certified:

- Native Windows install and GNS3 discovery test.

## Slice 11 AWS/SaaS Pilot Status

Implemented artifacts:

- `Dockerfile`
- `deploy/aws/netcode.env.example`
- `deploy/aws/docker-compose.pilot.yml`
- `docs/AWS_PILOT_READINESS_RUNBOOK_2026-07-08.md`

Validated:

- Docker is available locally.
- `docker compose -f deploy/aws/docker-compose.pilot.yml config` renders.

Not yet certified:

- AWS deployment has not been executed.
- RDS/EFS/ALB/Secrets Manager path remains a deployment task.

## UI Integration Updates For Claude Validation

Additional Rez UI/server work after the live proof:

- Added read-only Rez proxy endpoints:
  - `GET /api/netcode/runners`
  - `POST /api/netcode/readiness/devices`
- Updated the Netcode workspace UI to:
  - Show real runner count from `/api/netcode/runners`.
  - Show real device readiness from `/api/netcode/readiness/devices`.
  - Surface readiness failures instead of hiding them.
  - Poll runner jobs after dry-run/apply/verify/rollback/Ansible queueing.
  - Refresh the backend change record only after the runner proof lands.

Why this matters:

- A mid-level engineer sees what is safe to do next.
- The page no longer implies “connected” just because the changes API loaded.
- The page does not show fake topology, fake task progress, or static marketing data.

## Validation Commands

Netcode:

```bash
.venv/bin/python -m py_compile netcode/api.py netcode/runner_agent.py netcode/windows_runner_package.py
.venv/bin/pytest tests/test_platform_core.py tests/test_fleet.py tests/test_pipeline.py tests/test_rca_remediation_bridge.py -q
```

Result before Slice 12 live proof:

- `103 passed`

Rez:

```bash
.venv_sdk/bin/python -m py_compile server.py
git diff --check
cd ui && npm run build
```

Results:

- `server.py` compile: pass
- `git diff --check`: pass
- UI build: repo-wide TypeScript failures remain in unrelated files.
- No `NetcodeWorkspacePage.tsx` errors appeared in the build log after the UI changes.

## Honest Gaps

1. Full repo UI build is still red from unrelated existing TypeScript debt.
2. Windows runner package is built but not yet installed/tested on a real Windows GNS3 workflow.
3. AWS pilot artifacts are prepared but not deployed.
4. Ansible execution path is implemented but not live-certified because `ansible-playbook` availability on the runner was not proven.
5. Rez authenticated proxy smoke via curl was blocked by the unknown local admin password; browser-authenticated UI should use the same endpoints after login.

## Acceptance For Current Phase

Passed:

- Runner-backed automation loop works end to end.
- Human approval gate is enforced.
- Runner performs the device write.
- Verification reads live state.
- Rollback removes the lab change.
- Evidence record exists for UI and audit.
- UI now uses real runner/readiness/job state instead of static claims.

Not claimed:

- AWS production readiness.
- Windows runner certification.
- Live Ansible playbook execution.
