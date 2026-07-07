# Closed-Loop RCA → Netcode — Functional Build Plan (for Codex)

**Date:** 2026-07-07
**Problem being fixed:** the unified UI is coming out as **static mockups** (marketing panels, fake cards). The dashboard shows "CHG-4271 / Approval required" but there is no real change behind it. Stop building screens. Build the **wiring**, then let the UI render real state.

## The one rule that fixes this
> **No static mockups. Every card/row/panel renders a REAL object from a REAL endpoint. Definition of done for the whole loop = a single automated E2E test that: opens an incident → Rez produces a read-only RCA proposal → Netcode creates a DRAFT change with real generated commands + rollback → a (different) human approves → it applies to a lab device → verification passes → signed evidence links back to the incident. If you can't click it and get a real `change_id`, it isn't done.**

## You already built 80% of this — do NOT rebuild it
The drift closed loop is **already live-proven** this session:
`drift detected → fleet.create_remediation_rollouts() → per-device draft change → approval gate (requester≠approver) → canary → apply → verify → signed evidence.`
**The RCA→Netcode path is the SAME pattern with a different trigger.** Instead of a drift finding, the input is a Rez RCA proposal. Generalize the existing remediation flow; don't invent a new one.

Existing pieces to REUSE (validated this session — do not reimplement):
- Change creation: `create_desired_state_intent` / `/api/desired-state/plan` (intent → change → exact commands + rollback + blast radius).
- Remediation-from-finding precedent: `fleet.create_remediation_rollouts` (`/api/fleet/remediate`).
- Approval gate: `POST /api/change/{id}/approve` (requester≠approver, enforced).
- Write pipeline (gated): `/api/lab/dry-run | apply | rollback`, `/api/verify/intent`.
- Evidence: `GET /api/change/{id}/record`.
- Read-only Rez bridge: `/api/rez/runner-read` (mints only `read_` jobs — Rez has no write path; runner enforces read/write by action type).

## The non-negotiable guardrail (enforce in code + prove with a test)
> **Diagnostics has NO apply path. Rez can produce a *proposal* (data). Only an approved Netcode change writes. The RCA→change handoff is a DATA object, never a command execution.**
This is already enforced at the runner (the bridge only mints `read_` jobs; the write path `create_job` is reachable only from the change pipeline). The plan must ADD a test that asserts: a Rez RCA proposal can *create a draft change* but can *never* cause a device write except through `/api/change/{id}/approve` + the gated apply path.

---

## The contract (build this FIRST, backend-only, before any UI)

### 1. `RemediationProposal` (Rez emits this — a data object, not commands)
```
{
  incident_id, device_id (canonical), finding: "<root cause text>",
  evidence_refs: [...],            # read-only evidence Rez collected
  proposed: {
     change_type: "add_vlan|interface_config|acl_rule|custom_config|...",
     values: {...}                 # the SAME shape Netcode change types already accept
  },
  confidence: 0..1, source: "rez_rca"
}
```
Rez produces this at the end of an investigation. It contains NO executable command and NO credential.

### 2. `POST /api/changes/from-rca` (Netcode — NEW, but thin: wrap existing change creation)
- Input: `RemediationProposal`. Output: a real **draft** `change` (status `draft`/`needs_review`, `source=rez_rca`, `incident_id` linked).
- Internally: build the intent via the existing change-type registry → run the existing static plan (exact commands + rollback + blast radius). **Reuse `create_desired_state_intent` + `run_static_pipeline`.** Do not generate commands in Rez.
- The draft is NOT applied. It has no approval yet. It just exists in the change store and appears in `GET /api/changes`.

### 3. State machine (all real, all in the store — no UI state)
`draft(rez_rca) → validated(plan+gates pass) → approval_required → approved(requester≠approver) → applying(canary→batch) → verified → completed(+evidence)` — with `rolled_back` and `blocked` as terminals. This is the EXISTING workflow state machine; the only new entry point is `draft(source=rez_rca)`.

---

## Build order (small, each ends in a passing test — not a screenshot)

**Slice A — the backend contract (NO UI).**
`RemediationProposal` + `POST /api/changes/from-rca` reusing existing change creation. Test: post a proposal → assert a real draft `change_id` exists with real generated commands + rollback, status `draft`, `source=rez_rca`, `incident_id` set, and **not applied**.

**Slice B — Rez emits the proposal.**
At the end of a (read-only) Rez investigation, produce the `RemediationProposal` and POST it to Netcode. Test: an incident with a known root cause yields a proposal that creates a Netcode draft change.

**Slice C — the unified UI renders REAL state (this replaces the static mockup).**
In the Rez React shell (the unified host, `:4006`), the **Netcode module** calls the REAL Netcode backend API:
- "Active work" table = `GET /api/changes` (draft/validating/approval-required/applying) — live rows, real `change_id`, real status.
- "Rez drafts" = the `source=rez_rca` subset.
- Clicking a row shows the REAL generated commands + rollback + blast radius (`/api/change/{id}/record` / plan).
- **Delete every hardcoded card.** If the backend returns nothing, the table is empty — that's correct, not a reason to fake it.
*(Note: the `:8095` static HTML shell is dead for the unified product. But the `:8095` **API** is the backend the module calls. Don't confuse the two.)*

**Slice D — wire the human loop to real endpoints.**
Review → `POST /api/change/{id}/approve` (requester≠approver) → dry-run/canary/apply/verify via the existing gated endpoints → evidence posted back to the incident. The approval click is a REAL state transition, not a UI toggle.

**Slice E — the guardrail + E2E test (the definition of done).**
1. Guardrail test: assert Rez cannot cause a device write except via an approved change (attempt to drive a write through the bridge → blocked; only `/api/change/{id}/approve`+apply writes).
2. **The E2E test / the demo:** seed a real incident → Rez RCA (read-only) → proposal → draft change with real commands → approve as a second user → apply to a lab device → verify passes → evidence links back to the incident. This test IS the closed-loop demo. If it's green, the loop works. If it can't be written, the loop is still static.

**Slice F (LAST) — UI density polish.** Only after A–E are green: compact cards/tables, promote Workflow Packs to a primary path, connect the right-hand Live Outcome panel to the selected work item, reduce font sizes. Polish a *working* dashboard, never a mockup.

---

## Human-approval invariant (state it on every surface, enforce in code)
`Rez can recommend → Netcode stages a draft → engineer reviews exact commands + rollback → approval (requester≠approver) unlocks canary/apply → post-check verifies live state.` **Human approval is always required. Diagnostics has no apply path.** This is both the safety guarantee and the GTM story — keep the approval step *visible*, never hidden.

## How to tell Codex it's done (acceptance gates)
- [ ] `POST /api/changes/from-rca` creates a real draft change from a proposal (test).
- [ ] The Netcode module shows the REAL change queue (no hardcoded rows); empty backend → empty table.
- [ ] Approve is a real requester≠approver transition; apply/verify run the existing gated pipeline on a lab device.
- [ ] Guardrail test: Rez cannot write except through an approved change.
- [ ] **One E2E test walks incident → proposal → draft → approve → apply → verify → evidence and asserts a real `change_id` at each transition.** Green = the closed loop works.

## Why this unblocks Codex
It's failing because "build the unified dashboard" is a UI task with no anchor, so it produces marketing panels. This plan gives it an anchor: a **real object (`change_id`) that must flow through real endpoints**, proven by a test that static content can't fake. And it reuses the drift→remediation loop you already built and I already validated — so it's generalization, not invention.
