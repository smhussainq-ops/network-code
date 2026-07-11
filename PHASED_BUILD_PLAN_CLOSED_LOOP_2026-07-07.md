# Phased Build Plan — Rez → Netcode Closed Loop

**Date:** 2026-07-07
**Grounded in:** 6 code-level design passes across both repos + the founder's approval ruling.
**Companion:** `CLOSED_LOOP_RCA_TO_NETCODE_PLAN_2026-07-07.md` (the anti-static contract) and the validated hop status.

## North star
The loop closes with a human at the gate:
> **Incident → Rez read-only RCA → RemediationProposal (data) → Netcode DRAFT change → human reviews commands+rollback → dry-run → approve → apply → verify → signed evidence linking incident ↔ remediation.**
> **The machine proposes; the human disposes.**

## Invariant checklist — every phase must preserve all five
1. **Engineer = approval on human paths.** The interactive Shell is untouched — no added requester≠approver gate, no attach-to-approved tiering.
2. **Rez RCA is strict.** Its proposal is data-only. The draft it creates **never auto-applies, is never machine-approved, and is NEVER created in `dry_run_passed`** — it must pass a *real* dry-run before the approve gate opens. Requester = `rez_rca` (machine), so any human approver satisfies requester≠approver by construction.
3. **Device credentials never leave the runner.** Resolved from `~/.netcode-runner/inventory.yaml`; the cloud rejects creds (HTTP 400).
4. **Diagnostics has no apply path.** The Rez bridge mints only `read_` jobs; a "push to Netcode" is a *data* handoff, never a device write.
5. **Anti-static.** Every UI element renders a real `change_id` from a real endpoint. Empty backend → empty table, never a mock card.

## ⚠️ Design reconciliations (resolved before build — do not re-litigate)
The design passes disagreed on three points. Resolved:
- **Draft state:** the `from-rca` draft lands in **`draft`/`validated`**, NOT `dry_run_passed`. Rejecting the Spec-4 shortcut that pre-sets `dry_run_passed` — it would let the machine skip the dry-run proof (invariant 2). The human triggers the real dry-run; only then does `/api/change/{id}/approve` unlock.
- **Proposal shape:** the proposal carries **`change_type` + `values`** (mapped via the `change_types.py` registry) so the *existing pipeline* generates the exact commands + rollback + blast radius and can dry-run them. When the fix doesn't map to a registered type, **fall back to the existing `custom_config` change type** (already built) — raw commands still flow through templating, validation, rollback, and dry-run. **Never accept a raw-commands-only change that bypasses the pipeline.**
- **One endpoint, one identity:** the endpoint is `POST /api/changes/from-rca`; requester is `rez_rca`; `created_by_user_id = null`. Drop the parallel `/api/changes/from-rez` + separate `rez_remediation` change type from the design drafts — redundant.

---

## Phase map & critical path

| Phase | Title | Owner | Effort | On demo critical path? |
|---|---|---|---|---|
| **0** | Foundation (already built) | — | — | (rides on it) |
| **1** | Backend contract: `from-rca` → DRAFT | Netcode (me) | M | ✅ |
| **2** | Rez emits proposal + "Push to Netcode" | Rez UI (Codex) | M | ✅ |
| **3** | Human review/approve/apply/verify in unified UI | Shared | L | ✅ |
| **4** | Auto-wire verify-fail → Diagnostics | Netcode (me) | S | ❌ (makes it continuous) |
| **5** | E2E proof + 90-sec money demo | Netcode (me) | M | ✅ |
| **6** | Discovery "use for both" wiring | Netcode (me) | S | ❌ (pilot prerequisite) |

**Critical path to the money demo: 1 → 2 → 3 → 5.** Phases 4 and 6 run in parallel off the critical path; 6 must land before a pilot.

---

## Phase 0 — Foundation (already built; do NOT rebuild)
Discovery (`discovery.py:86`, creds stay local), the governed Shell (`shell_guard.py`), the full gated automation pipeline (`orchestrator.py` → dry-run → approve `api.py:1756` requester≠approver `api.py:1736` → apply → verify → evidence), fleet canary/auto-halt (`fleet.py:280`), the read-only Rez bridge (`api.py:896`, `store.py:810/819`), and the verify-fail handoff *builder* (`diagnostics_handoff.py`). The loop rides on these.

## Phase 1 — Backend contract: `POST /api/changes/from-rca` → DRAFT  ·  Owner: Netcode (me)
**Goal:** one new entry point that turns a Rez proposal into a governed DRAFT change, reusing the entire existing pipeline. The anchor Codex's UI hits.

**Work**
- `RemediationProposal` request model: `incident_id, device_id, site, finding, root_cause, evidence_refs[], proposed:{change_type, values}, confidence:{level,reason}, source:"rez_rca"`.
- **RCA→change_type mapping layer:** `root_cause` → a registered `ChangeTypeSpec` key + `values`; **fall back to `custom_config`** when unmapped; refuse `low` confidence (422).
- Handler: `create_desired_state_intent(..., requested_by="rez_rca")` (`orchestrator.py:57`) → `run_static_pipeline` (`orchestrator.py:96`) → persist via `get_or_create_change`/`update_change`; land in **`draft`/`validated`** (`workflow.py:114`), `created_by_user_id=null`.
- **Guardrail test:** assert a `from-rca` draft (a) is not applied, (b) is rejected at `/approve` until a real dry-run sets `dry_run_passed`, (c) is rejected at `/apply` until approved, (d) cannot be self-approved (requester `rez_rca` ≠ any human).

**Contract anchors:** new `POST /api/changes/from-rca` in `api.py` (confirmed `/api/changes` is GET-only today, `api.py:1839`); reuses `orchestrator.py:57/96`, `store.py:69/361/417`, approve gate `api.py:1756`, dry-run `api.py:628`, apply `api.py:642`.

**Acceptance:** post a proposal → a real draft `change_id` exists, static-validated, `source=rez_rca`, **not applied**; the guardrail test proves it can only reach the device via dry-run→approve→apply. No UI yet.

**Honors decision:** machine emits data only; draft never auto-applies; human is the sole approver by construction.

## Phase 2 — Rez emits proposal + "Push to Netcode"  ·  Owner: Rez UI (Codex)
**Goal:** the RCA card produces a structured proposal and drafts it in Netcode — never applies.

**Work**
- `POST /api/chat/{session_id}/remediation-proposal` (`resonance-core/server.py`): read the latest RCA conclusion from the session, extract root cause / remediation / evidence / scope / device, **infer `change_type`** (heuristics; `custom_config` fallback), emit the `RemediationProposal`.
- `RemediationProposalPanel.tsx` + a **"Push to Netcode"** button on the RCA card (`ChatInterface.tsx:430`, today read-only prose); preview the inferred change, then call Netcode `POST /api/changes/from-rca` with `NETCODE_CONTROL_PLANE_URL` + `NETCODE_REZ_BRIDGE_TOKEN` (already injected, `sdk_session_manager.py`).
- Thread `session_id` + RCA context through the store; on success show "Draft created: {change_id}" with a deep link into the Netcode module.

**Depends on:** Phase 1.

**Acceptance:** click "Push to Netcode" in chat-v2 → a real draft with a real `change_id` appears in Netcode, **not applied**; failure → a clear error, never a fabricated card.

**Honors decision:** Rez emits a proposal; the human approves in Netcode. The bridge token is machine-to-machine; it grants *draft*, not *apply*.

## Phase 3 — Human review/approve/apply/verify in the unified UI  ·  Owner: Shared (Codex UI + my endpoints)
**Goal:** the Netcode module becomes a real operations surface where a human closes the loop.

**Work**
- **Active Work** table = live `GET /api/changes` (real `id`/`status`/`source`); surface `source=rez_rca` drafts. **Delete every hardcoded card** in `NetcodeWorkspacePage.tsx`.
- Draft detail drawer = real `GET /api/change/{id}/record` (`api.py:1912`): generated commands, rollback, blast radius.
- Human loop: dry-run (`/api/lab/dry-run`) → **approve** (`/api/change/{id}/approve`, requester≠approver) → apply (`/api/lab/apply`) → verify (`/api/verify/intent`) → evidence. Approve is a real state transition, gated on `dry_run_passed`.

**Depends on:** Phase 1 (Phase 2 supplies live content).

**Acceptance:** a human takes a `rez_rca` draft `draft → dry_run_passed → approved → applied → verified`, every transition real and evidenced; the queue reflects real store state.

**Honors decision:** the human review + approve click is the gate; the machine never advanced past `draft`.

## Phase 4 — Auto-wire verify-fail → Diagnostics  ·  Owner: Netcode (me)
**Goal:** make hop 4 continuous — a failed change *offers* RCA automatically instead of waiting for a human to think of it.

**Work**
- On `failed=True` in the verify branches (`fleet.py` verify path + `/api/verify/intent` `api.py:1584`), POST to `/api/diagnostics/verification-handoff` (`api.py:1217`, builder already exists + tested `test_platform_core.py:1157`) with device, check, expected-vs-actual, `change_id`, evidence.
- Unified UI: a dismissible **"Investigate with Rez"** action pre-loaded with that context; opens Diagnostics with the failure as the initial frame.

**Depends on:** nothing new (builder exists). Off the demo critical path but completes the failure→RCA seam.

**Acceptance:** a deliberately failed verify auto-produces a handoff context and an offered investigate action; test asserts the POST fires on `failed=True` and the context carries expected/actual.

**Honors decision:** the handoff is read-only context (`direct_write_allowed=False`); it offers investigation, not a write.

## Phase 5 — E2E proof + 90-second money demo  ·  Owner: Netcode (me)
**Goal:** the single artifact that proves the loop — and the demo no competitor can screen-record.

**Work**
- `test_e2e_incident_to_remediation`: incident (intentional verify failure) → RCA read-only → `RemediationProposal` → `from-rca` draft → **real dry-run** → approve (requester≠approver) → apply on a lab device → verify passes → evidence links `incident_change_id ↔ remediation change_id ↔ verify timestamp`. **Assert a real `change_id` at every transition** (static content cannot pass it).
- `demo_remediation_flow` (90s): intentional VLAN failure → Diagnostics engages → RCA cites the exact failed condition from live read-only evidence → "Push to Netcode" → draft with real commands → **engineer clicks APPROVE** → re-apply → verify passes → evidence chain. The approval click stays in — it's the differentiator.

**Depends on:** Phase 1, 3 (the test can drive the APIs directly; the demo uses the UI).

**Acceptance:** the E2E test is green with real change_ids at each hop; the demo runs end-to-end with the human gate visible.

## Phase 6 — Discovery "use for both" wiring  ·  Owner: Netcode (me)
**Goal:** close the confirmed gap so one scan makes a device usable by *both* automation and Rez reads — no manual step.

**What's confirmed:** the runner scan writes runner-local inventory but sets `source_of_truth_written: False` (`runner_agent.py:966`) — it does **not** auto-import to the control-plane inventory (`inventories/lab.yaml`). Result today: a scanned device is usable by the Rez read bridge but **not** by the automation pipeline until someone calls `/api/source-of-truth/devices/import` (`api.py:1004`). A change targeting it rejects with "unknown device."

**Work:** after a runner discovery scan, auto-import the *public* candidate to the control-plane inventory (or have the UI call import inline on scan success). Public facts only — creds stay on the runner.

**Depends on:** nothing. Small, but a **pilot prerequisite** (removes silent friction).

**Acceptance:** one discovery scan → the device is immediately usable by a change plan AND the Rez read bridge, with no manual import.

---

## Sequencing & ownership summary
- **Me (Netcode backend, this repo):** Phases 1, 4, 5, 6.
- **Codex (Rez UI, resonance-core):** Phase 2, and the frontend half of Phase 3.
- **Shared:** Phase 3 (my endpoints already exist; Codex wires the UI to them).
- **Order:** ship **Phase 1 first** (it's the anchor — Codex can't drift into mockups against a real endpoint), then 2 → 3 → 5 for the demo; run 4 and 6 in parallel; 6 before any pilot.

## Definition of done (the whole loop)
The E2E test (Phase 5) is green, the 90-second demo runs with the human-approval click visible, and every dashboard element in the Netcode module is backed by a real `change_id` from a real endpoint. If you can't click an incident, get a real RCA proposal, see it as a draft with real commands, approve it as a human, apply it to a device, and see verified evidence — it isn't done.
