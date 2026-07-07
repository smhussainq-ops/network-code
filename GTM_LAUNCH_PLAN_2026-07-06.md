# Rezonance GTM & Launch Plan — CEO / Marketing / Sales

**Date:** 2026-07-06
**Author frame:** operator plan sitting *above* the website-conversion analysis. The conversion memo is good tactics; this decides what game we're playing and in what order.
**Reality check (grounding):** working POC (Netcode change-safety + Rez RCA, runner-split validated end-to-end), **zero customers, zero revenue, zero referenceable outcomes,** AWS pilot backend in flight. That reality drives everything below.

---

## 0. Blunt verdict on the conversion memo

I agree with ~80% of the website critique. Kill "$99" as the hero CTA, kill "Explore/See" passive verbs, put the wedge workflow above the fold, add "Python-grade NetDevOps. No Python required.", add "you're approving the next safe step, not launching a blind script." Those are cheap and correct — ship them.

**But the memo optimizes a funnel that has no traffic and no proof to put in it.** It designs a full product-led-growth (PLG) self-serve machine — free → test drive → $99 credit card → /start checkout. That is the right machine for *later*. Right now the bottleneck is not homepage conversion. It's **proof and pipeline.** Building a self-serve funnel before you have one quantified customer outcome is polishing the storefront of an empty shop.

**Sequencing principle for the next 90 days: Proof → Pipeline → Funnel.** The memo is the Funnel phase. You're two steps early to make it the priority — but the *cheap* parts of it are worth doing now.

---

## 1. What game are we playing? (the core strategic call)

Two motions are being conflated:

| | PLG self-serve (the memo's bias) | Sales-led / design-partner pilot |
|---|---|---|
| Motion | free → test drive → $99 card | founder outbound → paid pilot → land & expand |
| Buyer fit | low-risk, individual, fast | risk-averse enterprise network team, on-prem runner, security review |
| Revenue now | ~$0 (toy price) | real ($10–25k pilots) |
| What it's good for | **belief / lead-gen** | **money + the reference story** |

**Decision: PLG-*assisted*, sales-led.** The free/sample mode you already built (no creds, sandbox) is a phenomenal *trust and lead-gen* mechanism — a way to get an engineer to believe before they'll talk. But for a governed change-safety platform that runs an on-prem agent inside a customer's network and touches production devices, the **money motion is the paid pilot**, not a $99 checkout. Free builds belief; pilots close revenue; self-serve pricing waits for PMF.

Do not let the memo talk you into building a self-serve checkout as job #1. Build the *believe-it* sample and the *book-a-pilot* path. That's enough.

---

## 2. ICP — get painfully narrow

Not "network teams." Specifically:

> **Mid-market / lower-enterprise network teams (~200–5,000 devices), multi-site, CLI-native, no dedicated NetDevOps/software team, under audit & change-control pressure.**

- **Verticals that bleed most:** multi-site retail (stores), regional banks / credit unions, healthcare systems, regional MSPs. All have many similar sites, thin staff, real compliance, and dreaded change windows.
- **Champion:** the senior network engineer / network manager who owns the maintenance window and gets paged at 2am. Not the CIO.
- **Economic buyer:** their director / VP of infrastructure.
- **Blocker:** security / GRC (the on-prem runner story is how you disarm them — see §6).

Narrow ICP is not a limit; it's the whole point. One vertical, one repeatable pain, one reference story that rhymes across accounts.

---

## 3. Positioning — own an edge, don't out-platform the platforms

You will lose a feature war with Network to Code / Gluware / Itential. Don't fight on "automation platform." Own the seam none of them own cleanly:

> **The safe way for network engineers to automate and troubleshoot — without becoming programmers.**

Three pillars competitors can't claim *together*:
1. **No-code but real** — "Python-grade NetDevOps. No Python required." (This line from the memo is excellent. It names the exact ICP pain: CLI-native engineers under pressure to automate who can't/won't hire coders. Make it the spine of the messaging.)
2. **Safety/governance native** — canary → gates → approval → evidence → rollback. You built all of it. This is your moat vs. "here's a Python framework, good luck."
3. **Closed loop with RCA** — Netcode makes the change; Rez does read-only RCA when verification fails. Nobody pairs *governed change* with *read-only troubleshooting* like this.

Category line to test: **"Change-safety and RCA for network engineers."** Not "network automation platform" (crowded) — "the safe way to change and troubleshoot."

---

## 4. The wedge — one job, and a sharper sequencing than the memo

The memo is right that **one wedge beats eight equal cards**, and it picks **OS Upgrade Campaign**. Great pain: universally hated, recurring, high-stakes, quantifiable ROI (weekend windows, reload risk, rollback dread).

**But I disagree with making OS Upgrade the *free first experience*.** A blind fleet OS upgrade is the single scariest thing you could ask a risk-averse engineer to try first. Trust isn't there yet. Two wedges, two jobs:

- **Free front door = read-only RCA + Drift Check.** Zero risk (no writes, no config mode), instant value, it's a Trojan horse: get them addicted to *visibility* before you ask to *write*. This is what "sample mode, no credentials" should showcase first.
- **Flagship paid outcome = OS Upgrade Campaign.** This is the pilot, the case study, the ROI story — *after* trust exists.

So: **read-only visibility gets them in the door; the governed OS upgrade is what they pay for.** The memo made the free experience the scary write; flip it. Front door safe, paid outcome brave.

---

## 5. Pricing — the $99 is a strategic mistake

$99/month signals *toy* to a team running thousands of devices, and it isn't a real self-serve motion anyway. Recommendation:

- **Free (Community/Sample):** sandbox + read-only local. A lead magnet, **not** a revenue line. Best-for line: *"see the automation model before connecting real devices."*
- **Paid Design-Partner Pilot — $10–25k, scoped 60–90 days**, one real workflow (OS upgrade or golden baseline), with a written success definition. A paid pilot qualifies seriousness, funds you, and manufactures the reference. This is the money motion for the next two quarters.
- **Land-and-expand annual** priced on devices/sites, after a pilot succeeds.
- **Keep a small self-serve tier as a PLG *experiment*** if you like — but it is not the hero CTA and not the revenue thesis. Do not confuse funnel signal with a business model.

Drop "$99" from the nav. The primary asks are **Try the sample** (belief) and **Book a pilot** (money).

---

## 6. Trust is your #1 GTM asset and it's underplayed

This buyer's real blocker is security review. You have *already built* the answer and you're burying it:

> **Your credentials never leave your network. The cloud never touches your devices. Every change is gated, approved, and leaves a signed audit trail.**

The outbound-only runner, HMAC-signed results, credential scrubbing, requester≠approver gates — merchandise all of it. Ship a **one-page security/architecture brief** (the trust boundary diagram + the credential story). It shortens every enterprise deal because it pre-answers the GRC gate. This is a bigger conversion lever for *your* buyer than any hero-CTA wording.

---

## 7. Channels — go where this buyer actually lives

The website is not the engine early. The engine is:
1. **Founder-led outbound** to a hand-picked list of ~50 target accounts in one vertical, personalized around the upgrade / change-safety pain. Not "check out our platform" — "how are you handling your next OS upgrade window across your sites?"
2. **Network-engineering communities** where trust is peer-driven: r/networking, the Network to Code Slack, Packet Pushers, Cisco/Arista user groups, network-automation forums. Show up with *pain content and proof*, not ads.
3. **Pain content, not platform content:** "How we upgraded 184 switches without a weekend outage," "The read-only way to find config drift before it finds you," a teardown of a real change-gone-wrong. Show the product doing the scary thing safely (the 90-sec demo).
4. **Design partners → case study → repeat.** The flywheel.

---

## 8. The #1 thing that matters (say it plainly)

**You have no proof.** Everything above is theater until you have ONE quantified outcome story. The single highest-leverage activity for the next 60 days:

> **Land 2 paid design partners and manufacture one quantified outcome:** *"Upgraded N devices across M sites, zero outages, X engineer-hours saved, full audit trail."*

That story becomes the homepage hero, the outbound hook, the community post, the investor slide. **Website copy optimization is downstream of this.** Optimize the funnel after you have something true and specific to pour into it.

---

## 9. Metrics that matter at this stage (ignore vanity)

Track: (1) design-partner conversations booked, (2) paid pilots signed, (3) **do you have 1 referenceable outcome story — yes/no**, (4) pilot→paid conversion, (5) security-review pass rate.

Do **not** optimize: homepage conversion rate (no traffic), free signups (vanity until they're pilots), page-scroll depth. Those matter in the Funnel phase, not now.

---

## 10. Reconciling with the website memo — do this, skip that

**Do now (cheap, correct, timeboxed to ~3–5 days):**
- Kill "$99" and "Explore/See" as hero CTAs → **"Try a live sample"** + **"Watch the 90-sec demo"**, with **"Book a pilot"** secondary.
- Put the wedge above the fold; make **read-only RCA/Drift** the free sample and **OS Upgrade** the flagship section (per §4).
- Add **"Python-grade NetDevOps. No Python required."** section + the traditional-automation→workflow-pack mapping table (the memo's table is good).
- Add the safety line: **"You're approving the next safe step — not launching a blind script."**
- Make the first demo *smaller and safer* (readiness/drift), not the 96-device high-risk segmentation.
- Add the security/trust strip (§6) above the fold.
- Sticky CTA on scroll.

**Defer (the memo's PLG build — not yet):**
- Full `/start` self-serve checkout page and the 3-mode "download Community / start Starter" flow.
- $499 Team/MSP self-serve tier with credit-card conversion.
- Optimizing the full multi-section funnel architecture.
Build these once you have (a) the outcome story and (b) actual inbound traffic to convert. Building them now is effort spent on an empty funnel.

**Diverge from the memo:**
- Free experience = safe (RCA/drift), paid pilot = brave (OS upgrade). The memo inverted this.
- Pricing = pilots, not $99 self-serve.
- Don't spend weeks on the homepage. Days.

---

## 11. 90-day plan

**Days 0–30 — Proof foundation**
- Ship the AWS pilot backend (in flight; see AWS_PILOT_DEPLOYMENT_PLAN).
- Timeboxed homepage pass: the "do now" list in §10 only.
- Build the target list of 50 accounts in ONE vertical (recommend multi-site retail — you literally demo it with "stores").
- Write the 1-page security/architecture brief.
- Record the 90-sec demo: the scary thing (an OS upgrade / bulk change) done safely, ending in verify + rollback + evidence.

**Days 30–60 — Pipeline**
- Founder outbound to the 50, personalized on the upgrade/change-safety pain.
- Show up in 2–3 communities with pain content.
- Free sample live as the lead magnet.
- **Goal: 2 paid design-partner pilots signed.**

**Days 60–90 — The reference**
- Deliver the pilots (real devices, via the customer runner). Nail one OS-upgrade or golden-baseline outcome.
- **Manufacture the one quantified outcome story.**
- Rebuild the homepage hero around the real story + the first logo.
- *Then* consider the fuller PLG funnel from the memo — now it has proof and traffic to convert.

---

## 12. The one idea to build everything around

The memo says the organizing idea is *"Start with one guided workflow pack."* Close, but too generic. The sharper version:

> **"Automate and troubleshoot your network — without becoming a programmer, and without ever launching a blind script."**

Free front door: **see your drift, read-only, in minutes.** Flagship paid outcome: **the OS upgrade you dread, done as a governed campaign.** Trust anchor: **your credentials never leave your network.**

Get *those* above the fold and in every outbound touch — and land the two pilots. The funnel comes after the proof.
