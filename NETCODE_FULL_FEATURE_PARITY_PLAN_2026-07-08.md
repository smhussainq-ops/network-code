# Netcode Full Feature Parity Plan

**Date:** 2026-07-08  
**Purpose:** Review plan for Claude Code before more implementation.  
**Goal:** Make the native Rez-hosted Netcode module reach feature parity with the original Netcode spec without reintroducing static/demo-only UI.

## 1. Definition Of Full Feature Parity

Netcode is feature-complete only when this loop is real end to end:

```text
Discover once
  -> shared inventory/device state
  -> choose workflow pack or Rez RCA draft
  -> generate exact plan/commands/rollback
  -> validate policy and blast radius
  -> dry-run/canary
  -> human approval
  -> apply through runner
  -> verify live state
  -> evidence record
  -> drift watch
  -> Rez Diagnostics on failure
  -> Netcode remediation draft
```

Every UI card must be backed by a real API object: `device`, `change_id`, `job_id`, `rollout_id`, `incident_id`, `evidence_id`, or `workflow_event`. Static marketing panels do not count.

## 2. Current Grounded Status

### Built And Real

| Capability | Status | Grounding |
|---|---:|---|
| Outbound local runner | Built | `netcode/runner_agent.py`, `/api/runner/*`, HMAC result signing, local inventory |
| Runner-local credentials | Built | runner reads `~/.netcode-runner/inventory.yaml`; cloud strips/rejects credentials in runner mode |
| Discovery scan | Built | `/api/discovery/scan`, runner read action `discovery` |
| Manual source-of-truth import | Built | `/api/source-of-truth/devices/import` |
| Change type registry | Built | `netcode/change_types.py` |
| Native change types | Built | `add_vlan`, `interface_config`, `bgp_neighbor`, `acl_rule`, `site_device_intent`, `ntp_standardize`, `custom_config` |
| Workflow pack catalog | Built | `netcode/workflow_packs.py` |
| Intent rendering and static validation | Built | `netcode/orchestrator.py`, `netcode/validation.py` |
| Dry-run/apply/rollback endpoints | Built | `/api/lab/dry-run`, `/api/lab/apply`, `/api/lab/rollback` |
| Apply gate | Built | apply requires dry-run proof; tests exist |
| Approval gate | Built | `/api/change/{id}/approve`, requester-not-approver enforced |
| Fleet canary/batch rollout | Built | `netcode/fleet.py`, `/api/fleet/rollouts/*` |
| Drift watch/remediation rollout | Built | `netcode/drift.py`, `/api/fleet/drift/*`, `/api/fleet/remediate` |
| Git APIs | Built | `/api/git/status`, setup, branch, commit, push |
| Human shell | Built | `/api/shell/*`, `shell_guard.py`, `shell_pty.py` |
| Ansible planner | Built | `/api/workflow-packs/ansible/plan`, `netcode/ansible_backend.py` |
| Rez read bridge | Built | `/api/rez/runner-read`; read actions only |
| Rez RCA -> Netcode draft endpoint | Built | `/api/changes/from-rca`, commit `0178681` |
| Rez UI native Netcode route | Partial but real | `NetcodeWorkspacePage.tsx`, reads live `/api/netcode/changes` via Rez proxy |

### Partial / Not Yet Feature-Parity

| Capability | Current Reality | Gap |
|---|---|---|
| Native Netcode UI | Basic Rez-hosted module exists | Needs full workflow UX: details, plan, validate, dry-run, approval, apply, verify, rollback, evidence |
| Active work queue | Reads live changes | Needs row actions and detailed change drawer |
| Workflow packs UI | Catalog exists | Needs real pack selection and form flow inside Rez UI |
| Plan/command preview | Backend can produce artifacts | UI does not expose generated commands, rollback, policy gates, blast radius |
| Human approval flow | Backend exists | UI does not drive `draft -> dry_run_passed -> approved -> applied -> verified` |
| RCA push to Netcode | Button exists | It parses text in the browser; needs structured backend `RemediationProposal` |
| RCA mapping | Fallback to `custom_config` works | Needs root-cause-to-change-type mapping for safe known cases |
| Discovery once/use everywhere | Runner discovery works | Scan result does not auto-import public facts into control-plane source-of-truth |
| Verify-fail -> Rez Diagnostics | Handoff builder exists | Failure path does not auto-create/open a diagnostic handoff |
| Evidence chain | Change record endpoint exists | Need unified incident/change/job/verify evidence record in UI |
| Git-backed rollback UX | Git APIs and rollback commands exist | Need full rollback workflow: revert intent, reverse plan, validate, canary, apply |
| Ansible workflow execution | Planner exists | Runner-executed Ansible check/canary/apply path is not complete |
| Windows runner | Architecture supports outbound runner | Installer/service packaging and Windows validation pending |
| AWS/SaaS deployment | Plan exists | Not deployed/certified; local Mac still hosts current demo |
| Multi-vendor writes | Read adapters broad; write path lab/EOS-oriented | Need explicit vendor support matrix and production write adapters |

## 3. Non-Negotiable Guardrails

1. **No static parity claims.** If a UI element cannot trace to a real backend object, label it as placeholder or remove it.
2. **Rez stays read-only.** Rez can produce `RemediationProposal`; it cannot approve or apply.
3. **Netcode is the only write path.** All writes go through plan, validation, dry-run/canary, human approval, apply, verify.
4. **Human approval remains visible.** Do not hide the approve step in the demo. This is the enterprise trust story.
5. **Runner owns credentials.** SaaS/Rez/Netcode control plane never stores or forwards SSH/API secrets.
6. **One discovery spine.** A discovered device should be usable by Automation and Diagnostics without duplicate discovery.
7. **Adversarial review after each slice.** Verify no backdoor write path, no stale static UI, no direct CP device access, and no credential leakage.

## 4. Ordered Feature-Parity Slices

### Slice 0 — Baseline And Audit Gate

**Owner:** Shared  
**Goal:** Freeze current behavior and prevent more UI-only drift.

**Work:**
- Record current commits for both repos.
- Capture existing dirty/untracked runtime/generated files separately.
- Add a parity checklist test file or doc section that each slice updates.
- Define a smoke command set:
  - Netcode contract tests.
  - Rez backend syntax.
  - Rez UI browser smoke on `:4005`.
  - Runner split-mode smoke if runner is online.

**Acceptance:**
- Claude can validate the baseline with exact commit hashes.
- Dirty runtime artifacts are not confused with implementation.

### Slice 1 — Discovery Once, Use In Both Netcode And Rez

**Owner:** Netcode backend first, Rez UI second  
**Status:** Partial.

**Work:**
- On successful runner discovery, automatically create/import the public source-of-truth candidate.
- Preserve credential boundary: public facts only in control-plane inventory; secrets stay runner-local.
- UI should show one device list consumed by Netcode and Rez.
- Add tests for:
  - runner-mode discovery rejects submitted cloud creds.
  - scan result creates/imports public inventory candidate.
  - discovered device can immediately plan a Netcode change.
  - same device is usable by Rez runner-read.

**Acceptance:**
- One scan makes a device usable in workflow packs and Diagnostics without manual import.

### Slice 2 — Native Netcode Change Workspace In Rez UI

**Owner:** Rez UI with Netcode API support  
**Status:** Partial.

**Work:**
- Replace the remaining static Netcode module panels with live views:
  - active changes from `/api/changes`
  - detail from `/api/change/{id}/record`
  - workflow state from `/api/workflow/change/{id}`
  - jobs from `/api/jobs/{id}`
- Build a right-side detail drawer:
  - intent summary
  - generated commands
  - rollback commands
  - validation checks
  - blast radius
  - workflow events
  - apply/verify evidence
- Keep Shell as a separate human terminal surface, not mixed with machine automation.

**Acceptance:**
- Clicking any active work row opens real change details.
- No card in the Netcode module uses fake change data when Netcode CP is available.

### Slice 3 — Workflow Pack Selection And Planning UX

**Owner:** Rez UI + Netcode API  
**Status:** Backend built, UI pending.

**Work:**
- Render native workflow pack catalog from `/api/workflow-packs`.
- Render change-type form fields from `/api/desired-state/catalog`.
- Support at minimum:
  - golden baseline / NTP standardization
  - branch/site onboarding
  - controlled routing/ACL
  - custom config
  - Ansible plan preview
- Submit to `/api/desired-state/plan`.

**Acceptance:**
- User can create a real `change_id` from a workflow pack in the Rez-hosted UI.
- Plan artifacts are visible without leaving Rez.

### Slice 4 — Plan, Validate, Dry-Run, Approve, Apply, Verify UX

**Owner:** Shared  
**Status:** Backend mostly built, UI pending.

**Work:**
- Wire change detail actions:
  - run validation/static plan
  - dry-run
  - approve
  - apply
  - verify
  - rollback
- Enforce button availability from real workflow state, not UI booleans.
- Approval UI must expose requester and approver.
- Apply must remain disabled unless backend state permits it.

**Acceptance:**
- A real change can move through:
  `draft -> dry_run_passed -> approved -> rollback_available/verified`
- Attempted approve before dry-run fails visibly.
- Attempted apply before approval fails visibly.

### Slice 5 — Structured Rez RemediationProposal

**Owner:** Rez backend first, Rez UI second  
**Status:** Partial; current button parses RCA text in browser.

**Work:**
- Add backend `RemediationProposal` generation from the latest chat-v2 RCA:
  - `incident_id`
  - `target_device`
  - `site`
  - `root_cause`
  - `evidence_refs`
  - `confidence`
  - `proposed.change_type`
  - `proposed.values`
- Start with a conservative mapping table:
  - VLAN/trunk missing -> `interface_config` or `custom_config`
  - ACL missing/wrong -> `acl_rule`
  - NTP drift -> `ntp_standardize`
  - BGP neighbor issue -> `bgp_neighbor`
  - unknown -> `custom_config` with review-required metadata
- Refuse low-confidence or no-target proposals.
- UI previews proposal before creating Netcode draft.

**Acceptance:**
- RCA produces a structured proposal without frontend text scraping.
- Creating a draft from proposal produces a real Netcode `change_id`.
- Draft is not applied, not approved, and not `dry_run_passed`.

### Slice 6 — Verify Failure Automatically Offers Rez Diagnostics

**Owner:** Netcode backend + Rez UI  
**Status:** Builder exists; invocation pending.

**Work:**
- Wire failed verification paths to diagnostics handoff:
  - `/api/verify/intent`
  - fleet rollout verify branch
  - drift remediation verify branch
- Handoff includes:
  - `change_id`
  - device
  - expected state
  - actual state
  - failed check id
  - evidence refs
- UI shows “Investigate with Rez” when verification fails.

**Acceptance:**
- Induce a verification failure.
- A diagnostic handoff appears automatically.
- Opening it seeds Rez with the failed check context.

### Slice 7 — Evidence Chain And Audit Artifact

**Owner:** Shared  
**Status:** Partial.

**Work:**
- Create a unified evidence record that links:
  - incident id
  - RCA proposal id
  - change id
  - dry-run job id
  - approver
  - apply job id
  - verify result
  - rollback status
- Add UI evidence panel and export hook.
- Keep this as proof artifact for the 90-second demo and POC.

**Acceptance:**
- After a successful remediation, one screen/report proves:
  - what failed
  - what was proposed
  - who approved
  - what changed
  - what verified

### Slice 8 — Git-Backed Rollback UX

**Owner:** Netcode backend + Rez UI  
**Status:** APIs exist; full workflow pending.

**Work:**
- Show current branch/status for a change.
- Create or link change branch.
- Commit generated intent, commands, validation, dry-run, apply, verify artifacts.
- Add rollback flow:
  - revert to previous approved intent
  - generate reverse plan
  - validate
  - dry-run/canary
  - approve
  - apply
  - verify rollback

**Acceptance:**
- User can see Git state and rollback path for a real change.
- Rollback is a governed workflow, not a raw command button.

### Slice 9 — Ansible Pack Execution

**Owner:** Netcode backend and runner  
**Status:** Planner built; execution pending.

**Work:**
- Add runner-side Ansible execution mode:
  - check
  - canary
  - apply
- Require rollback playbook for canary/apply.
- Runner resolves inventory locally.
- Control plane never receives credentials.
- UI shows Ansible plan and blockers.

**Acceptance:**
- A playbook can be planned and checked safely.
- Apply is blocked without rollback playbook.
- Apply path still requires human approval and runner-local inventory.

### Slice 10 — Windows Local Runner Packaging

**Owner:** Netcode runner  
**Status:** Pending.

**Work:**
- Build Windows package:
  - installer or zip
  - service wrapper
  - enrollment command
  - local inventory import
  - log collection
  - auto-start
- Support outbound-only HTTPS/WSS to SaaS.
- Validate against a Windows-hosted GNS3 lab.

**Acceptance:**
- Windows user can install runner, enroll to Mac/AWS backend, import inventory, discover a device, run read checks, and execute approved automation.

### Slice 11 — AWS/SaaS Pilot Readiness

**Owner:** Shared  
**Status:** Plan exists; implementation pending.

**Work:**
- Containerize Netcode CP.
- Containerize/host Rez backend.
- ALB/TLS/WebSocket path.
- RDS/Postgres or documented SQLite pilot limitation.
- EFS/state for Rez runtime artifacts if needed.
- Secrets Manager for bridge/API tokens.
- Bedrock/IAM path for Rez.
- Runner enrollment flow against public URL.

**Acceptance:**
- Existing ORB runner connects to AWS backend over 443.
- Backend has no route to devices.
- Chat-v2 and Netcode automation still work through runner.

### Slice 12 — Full E2E Money Demo Certification

**Owner:** Shared  
**Status:** Pending.

**Scenario:**
1. Discover a lab device.
2. Create a workflow-pack change.
3. Dry-run and apply canary.
4. Verification intentionally fails.
5. Rez Diagnostics opens with failed check context.
6. Rez identifies RCA using read-only evidence.
7. Rez creates Netcode remediation draft.
8. Human reviews commands and rollback.
9. Human approves.
10. Netcode applies remediation.
11. Verification passes.
12. Evidence chain links incident -> remediation -> verification.

**Acceptance:**
- This is automated as a test or runnable script.
- This is recorded as the POC demo.
- No step relies on static content.

## 5. Recommended Execution Order

1. **Slice 1:** Discovery once/use everywhere.
2. **Slice 2:** Native live change workspace.
3. **Slice 3:** Workflow pack creation.
4. **Slice 4:** Human-gated change execution UI.
5. **Slice 5:** Structured Rez proposal.
6. **Slice 6:** Verify-fail diagnostics handoff.
7. **Slice 7:** Evidence chain.
8. **Slice 12:** First full loop demo on Mac/ORB.
9. **Slice 10:** Windows runner.
10. **Slice 11:** AWS pilot deployment.
11. **Slice 8:** Git rollback UX hardening.
12. **Slice 9:** Ansible execution.

Reasoning: the fastest path to POC is not every advanced feature first. It is a credible closed-loop demo with one or two workflow packs, real runner boundary, real human approval, real RCA handoff, and real evidence.

## 6. Claude Review Questions

1. Does Claude agree that Slice 1 is the next blocker for full parity?
2. Should `POST /api/changes/from-rca` be upgraded to run the existing static pipeline immediately, or remain draft-only until the human opens it?
3. Which first RCA mapping should be certified: ACL, VLAN/trunk, NTP, or NAT/firewall custom config?
4. Should we prioritize Windows runner before AWS, or prove AWS with ORB/Linux first?
5. Does Claude see any hidden write path from Rez to runner besides Netcode change workflow?

## 7. Stop Conditions

Stop and fix before moving to the next slice if any of these happen:

- A Rez path can write config directly.
- A Netcode UI card shows fake status while a real endpoint exists.
- A discovered device requires duplicate manual entry before automation/RCA can use it.
- A remediation draft skips dry-run.
- Apply is possible without approval.
- Approval can be performed by the requester.
- A runner job receives credentials from the control plane.
- An end-to-end demo step cannot be traced to a real ID.

## 8. Current Commits To Validate

Netcode:
- `0178681 api: create Netcode drafts from Rez RCA`

Rez:
- `41a89ab ui: wire Netcode module to Rez RCA drafts`

Known dirty/generated Netcode files are intentionally excluded from this plan and should not be mixed into feature implementation commits.
