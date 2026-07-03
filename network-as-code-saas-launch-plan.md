# Netcode SaaS Launch Plan

Date: 2026-07-03. Based on parallel market/competitive/architecture research
(5 research threads, ~115 sources, 2024–2026 vintage) synthesized against the
current state of the codebase. Companion to `network-as-code-platform-roadmap-v2.md`.

## Verdict: real market, confirmed wedge, crowded and hype-inflated category

**The pain is measured, not imagined.** Gartner: 65–67% of network changes are
still manual, with ~2% error rates. Uptime Institute 2025: ~45% of
network-related incidents trace to config/change-management failure. Opengear:
84% of CIOs report rising outages. Defensible market size: ~$7–8B (2025) core
network automation at ~9–10% CAGR (the $30B+/20% headlines are scope inflation).

**The wedge is confirmed unoccupied.** Across all four competitive clusters —
enterprise orchestration (Itential, Gluware, NetBrain), digital twins
(Forward Networks, IP Fabric), cheap NCM (Unimus, Netpicker, BackBox,
ManageEngine), and source-of-truth (NetBox, Infrahub) — **nobody sells a
Terraform-Cloud-like git-native plan → policy → dry-run-proof → gated-apply →
evidence workflow for CLI devices at mid-market prices.** Mid-market NCM tools
stop at backup/compliance-read or unguarded bulk push; digital twins are
six-figure and read-only; Nokia EDA legitimizes git+dry-run but only for DC
fabrics at operator scale.

**The honest caveat:** the real competitor is $0 — 64% of teams run homegrown
scripts, only 18% call their automation successful, and Gartner predicts 80% of
comprehensive initiatives shelved by 2028. Buyers demonstrably pay in this
segment (Auvik: 6,300+ customers on per-device SaaS with an on-prem collector;
NetBox: $35M Series B), but the modal buyer currently spends nothing on this
workflow. Sell to the buying trigger — post-outage, pre-audit — not to
automation aspiration.

## Positioning

**Terraform Cloud for physical network devices** — the change-safety layer that
lets a CLI-native team cross the read-only automation wall without hiring
programmers. Every change (typed or pasted raw config) gets an exact CLI diff,
blast radius, and rollback commands *before* apply; is gated by fail-closed
policy and an on-device dry-run proof; and emits one signed, audit-grade
evidence record. Device credentials never leave the customer's network.
Positioned against the status quo (Ansible scripts + screenshots pasted into
tickets), not against enterprise orchestration. Priced below one engineer's
salary: "the fully-funded automation project."

## Target segments (ranked)

1. **Arista-heavy mid-market (50–500 devices) with compliance pressure** — the
   only segment today's write path serves; design partners come from here.
   NetBox-running NetDevOps teams are the warmest subset.
2. **PCI-scoped branch/retail chains** — sharpest pain/feature fit (fail-closed
   segmentation policy + per-change evidence replaces screenshots-into-
   ServiceNow), but Cisco estates: gated on IOS-XE write. Sell 11-vendor
   read/drift/audit as the on-ramp now.
3. **MSPs** — proven per-device buyers; evidence records are a resellable
   compliance deliverable. Hard-require multi-tenancy + multi-vendor write:
   phase 2/3, not launch.
4. **DIY shops wanting a safety/audit layer over existing Ansible** —
   positioning-led expansion, not initial roadmap.

## Competitive wedge (what nobody else has)

- **On-device dry-run proof gating apply** — stronger than offline digital
  twins, and no mid-market tool has any plan/dry-run/gate pipeline at all.
- **Per-change signed evidence record** (intended diff → policy verdict →
  dry-run proof → applied diff → live verification) — no product at any tier
  emits this; today's mid-market audit evidence is screenshots.
- **Fail-closed policy incl. "credentials can never be pushed"** — answers the
  near-universal security unease with homegrown automation (EMA).
- **Usable by CLI-native engineers, zero Python** — typed changes + "paste any
  config with mandatory rollback" sidesteps the skills gap that kills DIY.
- **DIY fragility is citable:** AWX frozen (July 2024), Ansible 12 network
  modules broken across three releases, Nornir 16-month release gap,
  single-maintainer Oxidized, Batfish Enterprise dead.
- **Credential custody by construction** — outbound-only runner; browser/SaaS
  structurally cannot touch devices.

## Runner architecture blueprint (ordered)

1. **Pure outbound client**: TLS/443 long-poll job delivery; never listens.
   Publish a one-line egress allowlist (the single biggest adoption unlock —
   the HCP-agent/GitHub-runner/Datadog/Teleport pattern). Cut along the
   existing job-shaped runner abstraction.
2. **Two-phase identity**: single-use join token scoped to a runner pool →
   runner generates keypair locally → short-lived auto-renewing mTLS certs
   (Teleport model). Per-runner revocation.
3. **Credentials never transit the cloud, structurally**: creds live only on
   the runner (OS keyring/Vault/customer KMS); cloud stores opaque handles;
   startup self-check refuses to run otherwise.
4. **Signed job specs verified on-runner + fail-closed policy RE-RUN locally**
   — threat model explicitly names "compromised control plane" (self-hosted
   runners are documented backdoor vectors).
5. **Differentiator stays runner-side**: dry-run executes locally; apply gates
   on the locally-captured proof; cloud sees artifacts, never live sessions.
6. **Evidence signed at source** by runner identity, uploaded outbound,
   committed to the git change branch — signing upgrades a log to audit-grade.
7. **HA via stateless runner pools** (single-job runners, N = concurrency +
   redundancy; default 2/site; pool-per-tenant gives MSP isolation free).
8. **Signed auto-update with rollback**, version pinning, signed offline
   bundles for air-gapped shops.
9. **Placement/hardening docs**: management VLAN/OOB like a bastion; non-root;
   per-site device scoping; reference segmentation diagrams.
10. **Security-review package**: SBOM + CVE scanning, SLSA provenance, signed
    binaries, CIS-hardened defaults, annual pen-test attestation, published
    threat model.
11. **Failure-domain doc (the Meraki lesson)**: cloud down → devices
    unaffected; in-flight changes abort safely via session-abort discipline;
    git remains source of truth.

## Launch phases

| Phase | Timeframe | Goal | Exit criteria |
|---|---|---|---|
| **0: SaaS-able foundation** | months 0–4 | Postgres, auth/RBAC, basic multi-tenancy, runner extracted as outbound agent; SOC 2 + registry refactor started | One external pilot runs a real change through hosted control plane + on-prem runner; creds never leave their network |
| **1: Design partners (Arista)** | months 3–9 | 3–5 partners (discounted, not free), signed specs/evidence, NetBox read integration | 2+ referenceable cases where an auditor/CAB accepted Netcode evidence records; SOC 2 Type I in hand |
| **2: Cisco write + paid launch** | months 8–15 | IOS-XE write with identical proof discipline; public launch; per-device pricing + free read-only tier | 10+ paying customers; SOC 2 Type II delivered; mixed Arista+Cisco estate in production |
| **3: MSP channel + breadth** | months 14–24 | MSP edition (per-tenant pools, volume pricing, white-label evidence); Junos write; deeper NetBox/Infrahub integration | 2+ MSPs with 3+ end-client tenants; MSP ARR a second revenue line |

## Engineering gaps (ranked)

1. **Cisco IOS-XE write** — the single biggest gap vs demand (candidate-config/
   configure-replace maps to the dry-run discipline; Junos commit-confirm next).
2. **Auth/RBAC/multi-tenancy** — precondition for any SaaS revenue; bulk of the
   SOC 2 control surface.
3. **Runner extraction + hardening** — where the trust story lives.
4. **SQLite → Postgres** — mechanical, blocking, do before any external pilot.
5. **Change-type registry refactor** (~10 copy-paste ladders) — pay before
   vendor #2 or it multiplies.
6. **Compliance engineering** — SOC 2 ($40–80k yr 1; 3-month observation window
   is irreducible), trust center, whitepaper, pen test.
7. **NetBox integration** — read the de facto SoT; credibility + distribution.

## Pricing hypothesis

Per-device annual subscription in the empty corridor between backup commodity
and enterprise orchestration: **~$30–80/device/year** ($2.50–7/device/month).
A 200-device shop = ~$6–16k ACV; 400 devices = ~$12–32k — a manager-level
decision, decisively below one engineer's salary. **Free forever tier**:
read-only plan/diff/drift/backup on the 11-vendor read drivers up to ~10–25
devices (the proven Netpicker/Gluware land-grab). Monetize write execution,
RBAC/policy packs, evidence retention/export, multi-tenancy. MSP SKU
~$2–5/device/month with volume tiers. Two hard rules: no Terraform-style
metering complexity; no repricing shocks (HCP RUM churn / HashiCorp BSL fork
are the canonical goodwill-destruction cases).

## Risks (ranked)

1. **The $0 competitor** — buyers may keep not-buying; sell to post-outage/
   pre-audit triggers, lead with compliance evidence, keep ACV manager-level.
2. **Arista-only write in a Cisco world** — gates two of four segments until
   IOS-XE ships.
3. **Adjacent convergence** — NetBox Assurance ($55M, community gravity) is one
   step from execution; Infrahub ($14M) has AI framing; Nokia EDA could move
   down-market. Integrate with NetBox/Infrahub so they're channel, not rival.
4. **Asymmetric trust failure** — one bad apply at a design partner kills the
   category claim; the safety spine must stay the most-tested code in the company.
5. **Runner as backdoor** — existential if breached; signed-spec + local-policy
   + revocable identity + pen tests + published threat model.
6. **Compliance drag** — start SOC 2 now; Type I is the bridge.
7. **GTM overreach** — one motion first (sales-assisted, free tier as pipeline);
   vendor breadth follows validated demand.
8. **Honest ceiling** — the wedge is a subsegment of ~$7–8B growing ~10%: a
   credible $10–30M ARR trajectory. Anchor fundraising on Auvik's proven motion
   and NCCM's ~16% CAGR, not $30B headlines.

## First 90 days

- **Wk 1–2**: Start SOC 2 (Vanta/Drata-class); begin design-partner funnel
  (list 60–100 Arista-running orgs; mine Arista community, NAF/AutoCon, NetBox
  community; expect 3–5 partners from 40–200 conversations).
- **Wk 1–6**: Postgres migration + change-type registry refactor (neither
  touches the safety spine).
- **Wk 3–10**: Extract the runner (long-poll, two-phase enrollment, local
  secret store, signed specs verified on-runner, local policy re-check) +
  minimal auth/RBAC for a hosted pilot.
- **Wk 4–8**: Write the two documents that close pilots before certification:
  security whitepaper + failure-domain doc.
- **Wk 6–12**: First external pilot — one friendly Arista shop, one real
  production change, one signed evidence record their change process accepts.
- **Wk 8–12**: NetBox inventory integration.
- **Throughout**: do NOT start IOS-XE write until Arista partners validate
  willingness-to-pay; draft the pricing page early and test it in every
  design-partner conversation.
