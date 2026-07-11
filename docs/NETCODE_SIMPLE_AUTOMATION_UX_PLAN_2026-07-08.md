# Netcode Automation UX — Make It Simple, Bring Back the Shell, Cut the Noise

**Date:** 2026-07-08
**Author:** Claude Code (grounded in a live first-run walkthrough of the Rez-hosted UI as "Marcus," a mid-level engineer new to automation)
**For:** Codex

## The thesis (founder direction — this is the spec)
1. **Automation must be SIMPLE.** One linear flow: **pick the job → define it or use a template → select devices → push → validate → rollback if needed.** A wizard, not a workspace.
2. **The Shell is a core differentiator and it's missing.** Bring the governed Netcode Shell back into the unified UI.
3. **Netcode is cramped — too much data a mid-level engineer won't understand.** Strip every screen to what informs the next decision.

**The bar:** Marcus, on his first day, can push a **bulk change to N devices, validate it, and roll it back** — without a demo, without reading docs, without seeing a number he can't explain.

---

## Part A — The simple automation flow (the new spine)

Replace the single cramped "Change workspace" with a **5-step guided wizard**, one decision per screen, a persistent progress rail, and exactly one primary button per step. It sits on top of the **backend that already works** — do not rebuild the pipeline; reshape the UI over it.

### Step 1 — "What do you want to do?" (job picker)
Big, plain cards (not three dropdowns at once):
- **Upgrade OS / firmware** *(new capability — see Part D; ship as "coming soon / staged" if not ready, don't fake it)*
- **Bulk config change** → pick a template or define your own
- **Apply a template** → Golden baseline · NTP standardization · ACL update (from `/api/netcode/workflow-packs`)
- **Define a change** → paste CLI or fill a change-type form (`custom_config` or a registry type)

### Step 2 — Define it
- Template chosen → show only that template's few fields (from `/api/netcode/desired-state/catalog`).
- Define-your-own → one field for the change type + minimal inputs, or a paste box (`custom_config`) with a **rollback field** (required unless explicitly acknowledged).
- No Ansible card, no workflow-pack dropdown, no change-type dropdown all visible at once.

### Step 3 — Select devices
- A real device list from inventory/readiness (`/api/netcode/readiness/devices`), **multi-select** with search + "select all in site."
- Show **how many devices** and a **blast-radius line** ("12 devices across 2 sites").
- Grey out / warn on devices that failed readiness (reuse the honest readiness signal that already works).

### Step 4 — Push (safely)
Show the generated plan (exact commands + rollback + risk), then a simple gated sequence with one primary action at a time:
`Plan → Dry-run (proof) → Approve (2nd engineer) → Apply`.
- Single change: `desired-state/plan` → `lab/dry-run` → `change/{id}/approve` → `lab/apply`.
- **Bulk: use fleet canary→batch** (`/api/fleet/rollouts` + approve + start — **NOT currently proxied, see Part C**). Show canary → batch waves with auto-halt.
- Keep the existing "NEXT SAFE ACTION" panel — it's good — but make it the *only* status the user must read.

### Step 5 — Validate & rollback
- Verify live state (`/api/netcode/verify/intent`) → green pass / red fail.
- If bad: **one-click Rollback** (`/api/netcode/lab/rollback`), gated + evidence-backed.
- Link to the one-page evidence record (`/api/netcode/change/{id}/record`).

**Acceptance:** Marcus completes "apply NTP template to 8 devices → verify → roll back" end-to-end from the wizard, never touching a raw dropdown or an unexplained metric. Every step shows a real `change_id`/`rollout_id` from a real endpoint.

---

## Part B — Bring back the Shell (the differentiator)
The governed Netcode Shell (change-safe SSH: config locked until a change is attached, creds resolved at the runner, dangerous commands re-confirm, full transcript → evidence) exists in Netcode (`/api/shell/*`, `/api/runner/stream`, `shell_guard.py`, `shell_pty.py`, vendored xterm.js) but is **not surfaced in the unified UI** — no nav item, no Rez proxy. (Rez's own `_open_paramiko_shell` in `server.py` is a raw diagnostics shell, NOT the governed one — do not confuse them.)
- Add a **"Shell" nav item** in the unified UI that mounts the governed terminal.
- Proxy/bridge the shell WebSocket so the browser ↔ Rez ↔ Netcode CP ↔ runner ↔ device path works (creds stay on the runner; CP stays a broker).
- Position it exactly as before: **"the safest way to use SSH in production"** — read-only until a change is attached; attach a change to unlock config; every keystroke recorded to the change's evidence.
**Acceptance:** Marcus opens Shell, connects to a device, is blocked from `conf t` until he attaches a change, and his session shows up in that change's evidence.

---

## Part C — Cut the noise (declutter + kill nonsensical data)
Observed live (first-run walkthrough). **Rule: if an element doesn't inform the engineer's next decision, remove it or move it to Advanced/Evidence.**

**The front door (Ops Dashboard) is the worst offender — it's a Rez vanity dashboard, not an automation start:**
- **"Autonomous Tier 3 Engineer"** — contradicts the human-in-the-loop positioning. Remove/reword.
- **Device count contradicts itself:** "25 devices" and "12 devices" on the same screen (Inventory shows 25). Fix to one true number.
- **Seeded / undecodable metrics:** Network health 85%, Cases 28, "79% accuracy" beside "0 verified", "SSH corpus 1832", "Artifact coverage 28%", "Customer profile records 39", "Chat + audit lines 0". None inform a decision — cut or move to an Advanced diagnostics view.
- **Broken string:** "VRF segmentation: Et11,, Et9,." — malformed template output on the landing page. Fix or hide.
- **New front door:** the first screen a first-timer sees should be **"Start automation" (wizard Step 1) + a small honest status strip** (runner online · readiness 25/26 · 1 needs attention) — nothing else.

**Inside the Netcode module:**
- **All four tabs render the SAME screen (audit-confirmed).** `NetcodeWorkspacePage` sets a `mode` (Workspace/Verify/Drift/Rollback) but has **zero `mode ===` conditional rendering** — Verify, Drift and Rollback are cosmetic clones of Workspace. Either make each mode render its real content (Verify = the evidence chain for the selected change; Drift = live-vs-intent findings + reconcile CTA; Rollback = the rollback control) or remove the tabs until real. Don't imply features that aren't there.
- **Change-detail data bug:** a change titled "Register v2-**store4**" shows DEVICE "v2-**store1**" and SITE "site-101" (title/device/intent-path disagree). Fix the device/site population so they're consistent.
- **Fake defaults submitted as REAL data (governance hole):** `planSite` defaults to `site-101` and — worse — `approverName` defaults to `second-engineer`. The requester≠approver gate is real in the backend, but the UI hands the requester a pre-filled fake second approver, so **one person can satisfy the "second engineer" gate by accepting the default.** Make approver a **required real user** (no default); block submit on any placeholder site/device/approver. ("site-101" also isn't a real site — real sites: dc/hq/inet/mpls/s1/s2.)
- **Readiness doesn't gate anything** and **evidence reads like raw tokens** ("job abc1234", "status: blocked", "unknown"). Gate "Start a change" on green readiness; render each evidence step as a plain-English sentence.
- **Loading flash:** every sub-tab switch re-mounts and flashes "Checking / Loading / 0" for ~4s. Cache/skeleton so it doesn't look broken or empty.
- **Keep what's good:** the honest readiness banner (names the failing device + Recheck), the "NEXT SAFE ACTION" panel, the Guardrail card, and the live change-detail record are strong — reuse them *inside* the wizard, don't discard them.

**One inventory, one discovery:** today there are two (Rez "Digital Twin" + the runner-local inventory the wizard targets). Decide which is the source of truth for automation targets and make Step 3 read that one. Onboarding a *new* device for automation must be a single clear path (Story 2), not split across "Discovery" and "Inventory" Rez tabs.

---

## Part D — Honest capability gaps to build (don't fake these)
1. **OS upgrade** — no `os_upgrade`/`image_upgrade` change type exists. This is the flagship GTM job. Build it as a real staged workflow (pre-checks → image transfer → install → reload window → verify → rollback) or clearly label it "staged / coming soon" in Step 1. Do not present a fake OS-upgrade button.
2. **Bulk/fleet is not proxied.** The fleet canary→batch engine exists (`fleet.py`, `/api/fleet/rollouts`) but the Rez proxy exposes only single-change endpoints. Add `/api/netcode/fleet/*` proxy routes so Step 4 can do real multi-device rollouts.
3. **Drift not proxied/built in the unified UI** (Part C).
4. **`from-rca` P0s — NOW FIXED (verified in code 2026-07-08), only residual hardening left.** (a) approval is now **intrinsic**: `jobs.py:43-52 intrinsic_approval_required()` forces approval for `source=rez_rca` / `human_approval_required` even on an auth-off CP (`jobs.py:94`); (b) the `{**proposed}` credential pass-through is gone — `_intent_from_rca_proposal` builds a whitelist-only typed intent and recursively strips credential-shaped keys (`api.py:444-477`). 132 tests pass. **Residual (non-blocking):** add `model_config = ConfigDict(extra='forbid')` to `RcaRemediationProposalRequest`, plus a regression test asserting a `from-rca` draft can never reach apply without `workflow_state=='approved'` (auth-off).

---

## Sequencing for Codex
1. **Wizard Steps 1–5 over existing single-change endpoints** (template + custom_config paths). This alone gives Marcus a simple, complete flow. *(Highest leverage.)*
2. **Declutter Part C** in parallel — new simple front door, kill the nonsensical data, fix the change-detail device bug, de-stub or remove Drift.
3. **Proxy fleet endpoints** → turn Step 3–4 into real bulk canary→batch.
4. **Restore the Shell** (Part B).
5. **OS-upgrade workflow** (Part D-1) — the flagship, build it real.
6. **Close the 2 P0s** before the RCA path deploys.

## Definition of done
- A first-time engineer completes **bulk template change → select devices → push (canary→batch) → validate → rollback** from a guided wizard, with the Shell available, and **not one number on screen that they can't explain.** Every element traces to a real backend object; nothing implied that isn't wired.
