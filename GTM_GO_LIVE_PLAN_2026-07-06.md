# Rezonance GTM Go-Live Plan (Execution Runbook)

**Date:** 2026-07-06
**Companion to:** `GTM_LAUNCH_PLAN_2026-07-06.md` (strategy/positioning) and `AWS_PILOT_DEPLOYMENT_PLAN_2026-07-06.md` (infra). This doc is the *how/when/who* — the staged go-live with readiness gates and go/no-go checklists.

## Launch thesis (read this first)
This is **not** a big-bang public launch. With a working POC and zero proof, a Product-Hunt-style blast burns the one thing you can't get back — first impression — before you have a customer story. We run a **staged go-live**:

> **Quiet design-partner go-live NOW → public go-live only AFTER one proven, quantified customer outcome.**

Three gates, each with hard entry criteria. You do not pass a gate on vibes — you pass it on the checklist.

```
G0  Internal Readiness  ──▶  G1  Design-Partner Go-Live  ──▶  G2  Public Go-Live
    (can we show a          (2 paid pilots on real           (funnel + community,
     customer at all?)       devices, controlled)             backed by a real story)
```

---

## Roles (small team — name the owner, even if it's one person)
- **Founder (you):** outreach, pilot sales, positioning calls, the outcome story. The GTM owner.
- **Build (Codex / Claude Code):** AWS deploy, pre-pilot fixes, website changes, runner packaging.
- **Reviewer (Claude/adversarial validation):** gate checklists, security review, go/no-go sign-off.
- Everything below is tagged with an owner.

---

## GATE 0 — Internal Readiness
*Goal: nothing customer-facing goes live until we can safely put it in front of one real network team.*

### Workstreams & deliverables
| # | Workstream | Deliverable | Owner |
|---|---|---|---|
| 0.1 | **Infra** | AWS pilot backend live (ECS Netcode CP + Rez, RDS, EFS, ALB/TLS, Bedrock) per AWS plan | Build |
| 0.2 | **Pre-pilot fixes** | null-runner WS bug fixed; admin/admin123 + bootstrap defaults replaced with secrets; `NETCODE_REZ_BRIDGE_TOKEN` required; single-worker guardrail | Build |
| 0.3 | **Runner** | Linux runner enrolled against the *cloud* URL, proven end-to-end (device isolation capstone passes) | Build + Reviewer |
| 0.4 | **Trust** | 1-page security/architecture brief (credential-never-leaves + trust-boundary diagram) | Founder + Build |
| 0.5 | **Demo** | 90-sec demo recorded: a dreaded change done safely → verify → rollback → evidence | Founder |
| 0.6 | **Website** | "Do now" homepage pass (see §Website below) live | Build |
| 0.7 | **Pricing** | Design-partner pilot offer finalized ($10–25k, 60–90 days, written success definition); $99 removed as hero CTA | Founder |
| 0.8 | **Sales assets** | 50-account target list (multi-site retail); outbound templates; pilot one-pager + agreement | Founder |

### G0 → G1 GO/NO-GO checklist (all must be YES)
- [ ] Cloud backend reachable over HTTPS; both services healthy; a runner shows online.
- [ ] **Device-isolation capstone passes**: backend has no network path to devices; a Rez investigation + a gated change still complete through the runner.
- [ ] No default credentials anywhere; bridge token enforced; secrets in a manager (not env literals in an image).
- [ ] Security 1-pager exists and survives a skeptical read.
- [ ] 90-sec demo exists and is genuinely impressive (scary change made boring).
- [ ] Homepage "do now" changes live; no "$99" hero CTA; wedge above the fold.
- [ ] Pilot offer + agreement ready to send; target list built.
- [ ] A written **rollback/kill criteria** for a pilot exists (see Risks).

---

## GATE 1 — Design-Partner Go-Live (the launch that matters)
*Goal: 2 paid design partners running on their real devices, and one quantified outcome. This IS the go-live.*

### Launch-day runbook (per design partner)
1. **Kickoff call** — confirm the one workflow (recommend read-only RCA/drift first, then a golden-baseline or OS-upgrade as the paid outcome), success definition, timeline, security contact.
2. **Runner install** — customer installs the runner (service), enrolls with a single-use join token, imports **their full device inventory** (⚠️ inventory completeness is load-bearing — see the `v2-hq-edge-2` lesson: incomplete inventory silently degrades to snapshot data).
3. **Read-only first** — run RCA/drift across their fleet. Zero-risk, instant value, builds trust. *Do not write on day one.*
4. **First gated change** — one non-critical device: plan → approval → dry-run → apply → verify → evidence. Prove the safety loop on their hardware.
5. **The flagship outcome** — the OS upgrade or golden baseline they actually dread, as a governed campaign (canary → batch → verify), with you on the call.
6. **Capture the number** — instrument the outcome: devices, sites, time saved, outages avoided, audit artifact.

### Cadence during G1
- Weekly check-in with each partner; log every friction point.
- Founder personally present for the first real change on each partner.

### G1 → G2 GO/NO-GO checklist (all must be YES)
- [ ] **≥1 quantified outcome story** exists and the customer will let you reference it (logo or anonymized).
- [ ] ≥1 pilot converted (or committed) to a paid annual, OR a clear, dated path to it.
- [ ] A security review passed at least once (proves the trust story survives GRC).
- [ ] The onboarding runbook is repeatable (you could hand it to a second person).
- [ ] Product survived real-device use without a trust-breaking incident.

---

## GATE 2 — Public Go-Live
*Goal: turn the proven story into pipeline. Now — and only now — build the funnel.*

### Activities (backed by the G1 story)
- Rebuild homepage hero around the real outcome + first logo.
- Ship the deferred PLG funnel (the memo's `/start`, sample-mode self-serve, sticky CTAs) — now there's proof to convert against.
- Content launch: the customer story as a case study; a "how we upgraded N devices without a weekend outage" teardown; the read-only-drift piece.
- Community go-live: r/networking, Network to Code Slack, Packet Pushers, Cisco/Arista user groups — lead with the story and the demo, not the platform.
- Founder outbound scales from the proven message.

### G2 success metrics (30/60/90 after public go-live)
- Inbound design-partner conversations / week.
- Sample-mode activations → pilot conversations.
- Pilots signed; pilot→paid conversion.
- (Vanity metrics — homepage CVR, signups — become legitimate *here*, not before.)

---

## Website "do now" (Gate 0 scope only — the cheap, correct subset)
Reconciled with strategy (free=safe, paid=brave; no $99 hero):
- Hero CTA → **"Try a live sample"** (primary) + **"Watch the 90-sec demo"** (secondary); "Book a pilot" tertiary.
- Nav CTA → **"Try the sample"** (not "Start with $99").
- Wedge above the fold: **read-only RCA/Drift = the free sample**; **OS Upgrade Campaign = flagship paid section**.
- Section: **"Python-grade NetDevOps. No Python required."** + the automation→workflow-pack mapping table.
- Safety line: **"You're approving the next safe step — not launching a blind script."**
- First demo = small/safe (readiness/drift), not the 96-device high-risk segmentation.
- Trust strip above the fold: **"Your credentials never leave your network."**
- Sticky CTA on scroll.
**Deferred to Gate 2:** `/start` checkout, $499 self-serve tier, full multi-mode funnel.

---

## Metrics that gate the whole plan (not vanity)
| Phase | The one number that matters |
|---|---|
| G0 | Is the backend + capstone provably safe? (binary) |
| G1 | **Do we have 1 quantified, referenceable outcome?** (binary) |
| G1 | Paid pilots signed (target: 2) |
| G2 | Pilot→paid conversion + inbound pilot conversations |

Do **not** advance a gate to chase a lagging vanity metric. G1's binary outcome-story gate is the hinge of the entire company right now.

---

## Risks & kill/rollback criteria
- **A pilot change breaks a customer's network.** Mitigation: read-only first; first write on a non-critical device; founder present; canary-of-1; rollback pre-generated. Kill criteria: pause all writes for that partner, run RCA, root-cause before resuming.
- **Security review fails.** Mitigation: the 1-pager pre-clears it; lead with the runner/credential story. If it fails, treat it as product feedback — the gap is the roadmap.
- **No outcome story by day 90.** This is the real risk. Do not proceed to G2 without it — a public launch with no proof is worse than no launch. Re-run G1 with a different partner/workflow rather than force G2.
- **Over-building the funnel (Gate 2 work) before Gate 1 proof.** Explicitly forbidden by the gate structure. Build is only cleared for G2 scope after the G1 checklist is green.

---

## The one-sentence go-live rule
**We go live quietly to two paying design partners now; we go live publicly only when we can point at one true number a network engineer can't argue with.**
