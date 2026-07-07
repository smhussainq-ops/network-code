# Source notes — Netcode ↔ Rez unification (design input)

Grounded in the two repos (read 2026-07-07). This file is design reference, not a deliverable.
Repos: `smhussainq-ops/network-code` (Netcode) · `smhussainq-ops/resonance-core` (Rez).
Checkpoint tag in both: `checkpoint-2026-07-07-before-unified-ui-redesign`.
Rez `ui/src/App.tsx` carries a `// CODEX_CAN_WRITE` marker (their codex write-zone).

## Netcode (Network as Code) — the WRITE path
Static HTML/CSS/JS today (`static/index.html|styles.css|app.js`). Gated change chain:
source-of-truth → intent YAML → Jinja template → rendered EOS config → 7 policy checks →
Git review → lab dry-run (configure session … abort) → apply (commit) → verify (show …) →
rollback (no …) → evidence. Target: Arista EOS lab. Safety states: locked/ready/current/
running/passed/verified/blocked/rolledback.

## Rez (Resonance-core) — the READ-ONLY diagnostics brain
React/Vite, monolithic `ui/src/App.tsx` (~135KB) + `components/TwinGraph.tsx` (Cytoscape),
`components/Visualizers.tsx` (TopologyMiniMap SVG, TcpLadder). Tailwind dark (slate-900).
Top-level `mode: "console" | "pcap" | "twin"` (setMode). Backend `API_BASE=http://localhost:8000`.

Modes:
- console — discovery/topology console + RCA chat copilot ("Rez")
- pcap — upload .pcap → suspects, TCP physics, candidate RCAs, RezScore, DNA matches
- twin — Cytoscape Digital Twin: physical + logical_bgp edges, route_path trace, conflict
  overlays (state_atoms), node inspector, chaos injection (device/iface/neighbor down), lab status

Key types (data-contract gold):
- Suspect { host, role, score, description, evidence:Record<string,number>, primary_evidence_flow{client,server,client_port,server_port,retrans,dup_ack,packets,rst,zero_win} }
- AnalysisData { scan_info, summary, summary_text, candidate_rcas[], suspects[], domain_risk, primary_bottleneck, current_dna[], dna_matches[], hosts, topology, tcp_physics_summary }
- HistoryRow { id, timestamp, global_error_rate, max_surprisal, primary_rca }
- IncidentTab { id, label, analysis, createdAt, incidentId }
- ChatMessage { id, sender:"user"|"rez", text, toolEvents[] } ; ChatState { sessionId, messages[], caseId, contextSource, dnaTokens[], dnaMatches[], knownEntities }
- DiscoveryItem { id, incident_id, name, seed_node, depth, intent, node_count, link_count, site_count, created_at, metadata{context_source, nsg_path} }
- AttachedEnv { envIncidentId, contextSource, topologySnapshot, nsgContext, metadata }
- Twin context (from /context): nsg_graph{nodes,site_index}, device_states{<node>{interfaces{oper/admin_state,ip_prefixes},bgp_neighbors[]}}, topology_snapshot{links[]}, state_atoms[] (conflicts), metadata{seed,depth,intent,timestamp}
- RezScore: computeRezScore = candidate score*0.6 + topology(10) + history(10) + DNA memory(≤20), clamp 0–99

Rez API surface:
- GET /api/history?limit ; GET /api/analysis/latest ; POST /api/scan (multipart pcap_file+topology_filename)
- GET /api/discovery?limit ; GET /api/discovery/{envId}/context?view=snapshot|merged
- GET /api/discovery/{envId}/route_path?src=&dst=&dest_ip=&view= (→ steps[], path_nodes[], ok, reason)
- GET /api/lab/status (running, node_count, seed_ok, lab_name) ; chaos + nsg refresh endpoints
- localStorage keys: rez.active.discovery, rez.active.pcap, rez.discovery.engine (v1|v2), rez_discovery_seed

## Unification boundaries (from brief)
- Netcode = main shell (owns nav, identity, the write path).
- Rez = native Diagnostics workspace inside it; READ-ONLY (observes/diagnoses/recommends, never writes).
- Shared: Discovery, Inventory, Device/Topology state = one source of truth for both.
- Credentials: runner-local (local runner holds creds + does device I/O; shells never store creds).
- Netcode writes ONLY through gated workflows (plan→validate→apply→verify→rollback).
- Unified flow: Discover → Automate → Verify → Diagnose → Remediate/Rollback.
- Integration seam: Rez emits a RemediationProposal → Netcode ingests as a draft gated Change.
