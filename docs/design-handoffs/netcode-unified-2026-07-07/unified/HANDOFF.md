# Netcode × Rez — Unified UX: Implementation Spec (Codex handoff)

> **Status:** design/spec for review, then implementation. **Do not start coding before the boundaries in §0 are signed off.**
> **Companion:** `Unified UX Spec.html` (visual wireframes, same folder) — read it for the annotated screen layouts. This file is the machine-readable contract.
> **Baseline tag (both repos):** `checkpoint-2026-07-07-before-unified-ui-redesign`

## Repos & sources of truth
- **Netcode** (write path): `network-code` — static HTML/CSS/JS today (`static/index.html|styles.css|app.js`). Gated change chain: source-of-truth → intent YAML → Jinja → rendered EOS config → policy checks → Git → lab dry-run → apply → verify → rollback → evidence. Target: Arista EOS lab.
- **Rez / Resonance** (diagnostics): `resonance-core` — React/Vite. Committed truth is a monolithic `ui/src/App.tsx` (~135KB) + `ui/src/components/TwinGraph.tsx` (Cytoscape) + `ui/src/components/Visualizers.tsx` (TopologyMiniMap, TcpLadder). Backend at `http://localhost:8000`. `App.tsx` carries a `// CODEX_CAN_WRITE` marker.
- **Design system** (this project): tokens (`styles.css` → `tokens/*`), React components (`components/*`, namespace `window.NetcodeDesignSystem_071615`), and two reference kits (`ui_kits/netcode`, `ui_kits/netcode-platform`). Use these tokens/components as the shared UI layer.

---

## §0 · Principles & boundaries (INVARIANTS — do not violate)
1. **Netcode is the shell and the only writer.** Every device write goes through a gated workflow (plan → validate → apply → verify → rollback). No ad-hoc "run command" affordance may exist anywhere.
2. **Rez is read-only.** It observes, correlates, recommends. It must never write to a device. Enforced **server-side in the runner by op-type**, not only in UI.
3. **Shared source of truth.** Discovery + inventory + device/topology state are one dataset, produced once, read by both sides. No duplicate device/topology models.
4. **Credentials are runner-local.** Neither shell stores/sees creds. A local runner holds them and performs all device I/O, returning results + transcripts only.
5. **The only insight→action path is the seam:** Rez emits a `RemediationProposal` → Netcode ingests as a **draft** change → full gated path. Read-only never becomes a direct write.

---

## §1 · Information architecture (left nav)
```
Overview
  • Dashboard
Operate  (writes · amber)
  • Discovery        (shared)
  • Inventory        (shared)
  • Workflow Packs
  • Changes          (Plan → Verify → Apply → Evidence)
Diagnose (read-only · blue)
  • Diagnostics / RCA
  • Digital Twin
Records
  • Evidence & Audit
[footer] Runner status + active environment (lab/prod)
```
- Top bar: environment switcher (prod gated behind lab proof), global search, primary **New change** action.
- Right **Context panel** persists on every screen: current object status, expected vs happened, "device changed?", next safe action. On Diagnose screens it renders read-only + the **Create fix in Netcode** button.
- Grouping is by **intent (write vs read)**, not by product.

---

## §2 · Unified workflow
`Discover → Automate → Verify → Diagnose → Remediate/Rollback` — a **loop**, not a line.

| Stage | Owner | Writes? | Screens | Runner role |
|---|---|---|---|---|
| Discover | Shared | read | Discovery, Inventory | reads device facts + topology |
| Automate | Netcode | no (intent) | Workflow Packs, Change Plan | renders candidate; no device I/O |
| Verify | Netcode | gated | Verification | dry-run (`session … abort`) |
| Diagnose | Rez | never | Diagnostics/RCA, Digital Twin | reads telemetry/pcap/state |
| Remediate | Netcode | gated | Change Plan → Verify → Apply | apply (`commit`) · rollback (`no …`) |

Edges: **Diagnose → Remediate** = `RemediationProposal` back into Automate/Verify (gated). **Verify → Diagnose** = auto-correlation pass after every apply to catch drift.

---

## §3 · Screens (implementation specs)
Each screen: purpose · regions · design-system components · data in · actions · boundary · done-when.

### WF-1 Shell
- **Purpose:** the frame everything renders in.
- **Regions:** top bar · left nav rail (§1) · main workspace · right context panel.
- **Components:** `NavRail`-style rail, `SegmentedTabs` (env), `OutcomePanel` (context), `StatusBadge`, `Button`.
- **Boundary:** frame is Netcode; Diagnose screens mount inside it with read-only panel treatment.
- **Done-when:** nav routes to all 7 sections; context panel + runner status persist; write CTAs disable when runner disconnected.

### WF-2 Dashboard
- **Purpose:** unified home bridging Operate + Diagnose.
- **Regions:** KPI row (health, open changes, open incidents, drift) · two columns (changes / incidents) · runner banner.
- **Components:** `OutcomeCard`, `StatusBadge`, `Table`, `Button`.
- **Data in:** `/api/changes`, `/api/incidents`, `/api/topology` health, `/api/runner/status`.
- **Actions:** open change · open diagnostics · drift → start remediation.
- **Done-when:** change state (amber) and incident state (blue) both visible; drift tile can launch a remediation change.

### WF-3 Discovery (shared)
- **Purpose:** one scan populates inventory **and** twin.
- **Regions:** scan setup (subnet/seed, depth, engine v1/v2, vendor auto-detect) · results table · import CTA.
- **Components:** `TextField`, `Button`, `Table`, `StatusBadge`.
- **Data:** produces `Device[]` + `TopologySnapshot`; emits `incident_id` (env) for the twin. Runner performs crawl.
- **Boundary:** reads devices; only write is to the shared store on **Import**.
- **Done-when:** merges Netcode inventory discovery + Rez `/api/discovery`; single screen; drift flag computed vs last snapshot.

### WF-4 Workflow Packs
- **Purpose:** library of parameterized gated workflows (a Pack = intent schema + Jinja template + policy checks).
- **Regions:** pack grid (Add VLAN, Add BGP peer, Interface change, ACL/prefix-list, Site intent, + "from Rez proposal").
- **Components:** `Card`/tile, `StatusBadge`, `Button`.
- **Boundary:** selecting a pack drafts intent only — no writes.
- **Done-when:** `add_vlan` generalized into the Pack model; packs are versioned Git artifacts; the "from Rez proposal" entry accepts a `RemediationProposal`.

### WF-5 Change Plan
- **Purpose:** Terraform-style preview.
- **Regions:** summary tiles (affected/changes/risk) · diff (before→after) · exact commands · blast-radius list · "Run validation".
- **Components:** `DiffView`, `CodeBlock`/`CommandTranscript`, `OutcomeCard`, `CheckRow`, `Button`.
- **Data:** produces `Plan {diff, commands, affected[], risk}` on the `Change`; reads devices for "before" only.
- **Boundary:** preview only — **no apply button here**.
- **Done-when:** diff + literal CLI shown together; risk enriched by shared topology; only forward action is Run validation.

### WF-6 Verification
- **Purpose:** the gates + proof; unlocks apply.
- **Regions:** verdict banner · 3 gate cards (policy / vendor syntax / lab dry-run) · dry-run transcript · Apply (red, confirm).
- **Components:** `GateCard`, `CheckRow`, `CommandTranscript`, `StatusBadge`, `Button` (danger).
- **Data:** produces `Validation` + `ApplyJob` + `Verification` records.
- **Boundary:** dry-run is the only device contact pre-apply; apply is explicit + gated; post-apply reads back intended vs actual.
- **Done-when:** apply disabled until all gates green; Diagnose auto-runs post-apply.

### WF-7 Diagnostics / RCA (Rez, read-only)
- **Purpose:** Rez's `console·pcap·twin` collapsed into one read-only workspace.
- **Regions:** incident list (left) · RCA center (primary RCA, suspects, candidate RCAs, TCP ladder, RezScore, DNA matches) · copilot chat (right) with **Create fix in Netcode**.
- **Components:** restyled `TwinGraph`/`Visualizers` (TcpLadder), `StatusBadge`, `CheckRow`, chat panel.
- **Data:** `/api/incidents`, `/api/analysis/*`, `/api/scan` (pcap). RezScore from `computeRezScore`; DNA from `dna_matches`.
- **Boundary:** entirely read-only; copilot tools read-only; the **only** exit to write is the seam button.
- **Done-when:** RCA renders from real analysis shape; "Create fix" emits `RemediationProposal`.

### WF-8 Digital Twin (Rez, read-only)
- **Purpose:** topology graph + route tracing + conflicts.
- **Regions:** Cytoscape graph (physical + BGP layers, down edges red, conflict overlays) · route trace · node inspector · simulate (lab-only).
- **Components:** `TwinGraph` (keep), inspector panel.
- **Data:** `/api/topology/{env}/context`, `/route_path`; device_states, `state_atoms`.
- **Boundary:** read-only; **chaos/simulate gated to lab env**; findings route out via `RemediationProposal`.
- **Done-when:** route trace highlights path + break reason; node select cross-highlights with RCA; chaos hidden in prod.

---

## §4 · Data contracts (TypeScript)
```ts
// ---- Shared (Discovery writes; both read) ----
interface Device {
  id: string; mgmt_ip: string; vendor: string; model?: string;
  os?: string; site?: string; groups?: string[]; role?: string;
}
interface TopologySnapshot {          // a.k.a. NSG context
  env_id: string;
  nodes: string[];
  links: { src: string; dst: string; src_if?: string; dst_if?: string;
           type: "physical" | "bgp"; status?: "up" | "down" | "admin_down" }[];
  sites?: Record<string, unknown>;
  device_states?: Record<string, {    // per node
    interfaces?: Record<string, { oper_state?: string; admin_state?: string; ip_prefixes?: string[] }>;
    bgp_neighbors?: { neighbor_ip: string; state?: string }[];
  }>;
  state_atoms?: string[];             // semantic conflicts
  metadata?: { seed?: string; depth?: number; intent?: string; timestamp?: string };
}

// ---- Rez (read-only; produces) ----
interface Incident { incident_id: string; label: string; created_at: string; }
interface Analysis {
  incident_id: string;
  summary_text?: string;
  primary_bottleneck?: string;
  candidate_rcas: { id?: string|number; label?: string; score?: number; fix?: string }[];
  suspects: { host: string; role: string; score: number; description: string;
              evidence: Record<string, number>;
              primary_evidence_flow?: { client?: string; server?: string;
                retrans?: number; dup_ack?: number; rst?: number; zero_win?: number } }[];
  domain_risk?: Record<string, number | { risk?: number; risk_score?: number }>;
  dna_matches?: { id?: string|number; label?: string; score?: number; fix?: string }[];
  rez_score?: number;                 // computeRezScore: score*0.6 + topo(10)+history(10)+dna(≤20), 0–99
}

// ---- Netcode (only writer) ----
interface Change {
  change_id: string; pack: string; targets: string[];
  desired_state: string;             // YAML
  plan?: { diff: DiffLine[]; commands: string[]; affected: string[]; risk: "low"|"medium"|"high" };
  validation?: { status: "pass"|"fail"; checks: { group: string; state: string; title: string }[] };
  apply_job?: { id: string; status: string; transcript: string[] };
  verification?: { intended: string; actual: string; match: boolean };
  rollback?: { commands: string[]; status?: string };
}
interface DiffLine { type: "add"|"remove"|"context"|"header"; text: string }

// ---- THE SEAM (Rez → Netcode; NOT a device write) ----
interface RemediationProposal {
  source: "rez"; incident_id: string; target_device: string;
  suggested_pack: string;                 // e.g. "interface_change"
  proposed_intent: Record<string, unknown>;
  rationale: string; confidence: number;  // 0..1
  evidence_refs: string[];                // pcap:… , twin:route_path , …
}                                         // → Netcode creates a DRAFT Change; must pass all gates.

// ---- Runner (only holder of credentials) ----
// POST /api/runner/exec { op:"dry_run"|"apply"|"rollback"|"read", change_id, targets[] }
//   → { transcript[], result, evidence_id }   // creds resolved locally; never in payload/response
// Rez may call op:"read" only. Netcode may call apply/rollback. Runner enforces by op-type.

interface EvidenceRecord {              // unifies both sides
  incident_id?: string; change_id?: string; apply_job?: string;
  verification?: string; artifacts: string[]; report?: string;
}
```

### API surface (one gateway; replaces Rez's raw `:8000`)
- **Shared:** `/api/discovery` · `/api/devices` · `/api/topology/{env}/context` · `/api/topology/{env}/route_path`
- **Rez (read):** `/api/incidents` · `/api/analysis/*` · `/api/scan` · `/api/lab/status` · `POST /api/remediation-proposals`
- **Netcode (gated):** `/api/packs` · `/api/changes` · `/api/plan` · `/api/validate` · `/api/apply` · `/api/rollback` · `/api/verify` · `/api/evidence`
- **Runner:** `/api/runner/status` · `/api/runner/exec`

---

## §5 · Migration plan (PR-sized slices)
**Shell decision:** build the unified shell as a **React app consuming this design system**. Rez's React/Vite stack is the host; Netcode's screens are re-implemented in React; static Netcode `app.js` becomes reference, not runtime.

| Phase | Build | Touch / create | Delete | Done-when |
|---|---|---|---|---|
| **0 Foundation** | React shell skeleton (rail, top bar, context panel) on DS tokens/components. Freeze at tag. | new shell app; import DS | — | shell renders empty routes; DS wired |
| **1 Shared spine** | Discovery + Inventory (one screen) | merge two discovery UIs; shared `Device`/`TopologySnapshot` store | duplicate discovery UI | one scan populates inventory + twin |
| **2 Write path** | Workflow Packs → Change Plan → Verification → Apply → Evidence | generalize `add_vlan` → Pack model; keep runner boundary | Netcode static shell/`index.html` | a change goes plan→verify→apply→evidence in the shell |
| **3 Diagnostics** | Embed Rez RCA + Twin, read-only, restyled | collapse `console·pcap·twin`→2 sections; gate chaos to lab | Rez top-level mode switcher; Vite boilerplate | RCA + twin render read-only in the shell |
| **4 The seam** | `RemediationProposal` → pre-filled Pack → gated change; auto Diagnose after apply; unified Evidence | `/api/remediation-proposals`; Evidence join | — | a Rez finding becomes a gated change; evidence links incident↔change |
| **5 Cleanup** | One API gateway; runner enforces read-only by op-type; shared incident store | consolidate APIs | Rez `localStorage` incident tabs; dead models; any non-gated "run" affordance | read-only guaranteed server-side; no duplicate models |

**Sequencing rule:** never move the write path before the shared spine; never wire the seam before Diagnostics is read-only-clean. Every phase must leave the app shippable — if a phase can't ship, split it. One slice per PR against the checkpoint tag.

---

## How to use this with Codex
1. Copy `unified/HANDOFF.md` (this file) — and optionally `unified/Unified UX Spec.html` for visuals — into **both** repos, e.g. `docs/unified-ux-spec.md`. Commit on a branch off the checkpoint tag.
2. Kickoff prompt for Codex:
   > Read `docs/unified-ux-spec.md`. Do **not** implement yet. First produce: (a) a file-level plan for **Phase 0 + Phase 1** only, listing exact files to create/modify/delete in this repo; (b) any questions where the spec is ambiguous against the current code. Respect the §0 invariants — especially: Rez stays read-only, credentials stay runner-local, and every device write goes through the gated path. Wait for my approval before writing code.
3. Review Codex's plan against §0 + §5, approve, then let it implement **one phase per PR**. After each PR, verify the "done-when" for that phase before proceeding.
