# Netcode Launch Plan — Safe-Change Cockpit

Date: 2026-07-05, v2 (revised after a three-critic adversarial review: skeptical
buyer, GTM operator, staff-engineer code audit — all verdicts "fix then ship").
Companion to `network-as-code-saas-launch-plan.md` (strategy) and the
evidence-graded PMF brief. This is the tactical plan: what we fix, build,
harden, package, and sell to get Netcode from "live-proven prototype" to
"launched product with paying design partners."

## Launch thesis (one line)

**Netcode launches as the network change assurance cockpit: every change gets
exact commands, exact rollback, fail-closed validation, on-device dry-run
proof, an approval-gated apply, live verification, and a signed evidence
record — with device credentials that never leave the customer's network.**

We are NOT launching a better Nautobot, a source of truth, an automation
platform, or a job runner. We own one moment: **the 30 minutes before and
after a risky network change.** Every scope decision is tested against that
sentence. The corollary the review made explicit: our claims are audited by
the most skeptical people in software — so every claim in the pitch must be
*literally* true in the code before a partner hears it.

## Where we actually are (honest inventory, 2026-07-05)

**Proven live** (real Arista cEOS containerlab, deployed control-plane +
on-prem-runner mode — session-proven; committing the proof artifacts to
`reports/` is a week-1 task so the claims are evidenced in-repo):

- The full golden path: intent → git branch → plan (exact commands, blast
  radius, forward + rollback) → 7-check fail-closed policy → on-device dry-run
  (EOS config session: candidate, diff capture, abort-on-dry-run, commit only
  on apply, full transcript) → gated apply → live verify → push → evidence
  record → rollback. Server-side state machine makes apply impossible before
  `dry_run_passed` (`workflow.py`, enforced in `jobs.py`).
- `custom_config`: paste ANY vendor CLI, rollback lines required (fail-closed),
  credential fragments rejected, verify-contains live post-check.
- Drift, two baselines: per-change (lifecycle-aware) and per-device (aggregate
  of committed intents). Out-of-band sabotage detected live; named the exact
  VLAN and the change it violated.
- Troubleshoot/Investigate: read-only Rez checks with expected-vs-actual,
  honest pass/review/fail, attach-to-evidence, fail-closed read deadlines.
- Runner spine: outbound-only, two-phase enrollment with hashed single-use
  join tokens, per-runner HMAC result signing, credential-free *change*
  payloads, local policy re-check.
- Orgs/users/RBAC (`NETCODE_AUTH`, default off). 49 tests (all mocked — the
  live proofs are manual sessions, not CI). Security whitepaper +
  failure-domain doc drafted.

**Trust debt — claims that are currently falsifiable in code** (found by the
code audit; each one would be found by a partner's security review too):

1. **Discovery credentials leak to the cloud.** `/api/discovery/scan` in
   runner mode puts device username/password in the job payload, persists it
   in `jobs.payload_json`, and returns it via `/api/jobs`
   (`api.py`, `store.py`). The *change* path is credential-free as claimed;
   the discovery path is not. Falsifies the headline until fixed.
2. **The runner's "local policy re-run" trusts the control plane.** The policy
   it enforces arrives *in the job payload* (`payload.get("policy_yaml")`);
   `POLICY_FILE` is defined but never read, and the hardcoded fallback for
   `custom_config` is an empty allow-list — a compromised control plane can
   ship an empty policy and push arbitrary CLI. Falsifies the threat-model
   claim in `FAILURE_DOMAINS.md` until fixed.
3. **"Signed evidence" is symmetric.** Runner HMAC secrets sit in plaintext in
   the control-plane DB, so the control plane can forge any runner's
   signature. An auditor's first question. Ed25519 is launch scope, not
   fast-follow — or the claim softens to "integrity-checked."
4. **No approval gate exists.** `approved`/`approval_required` states exist in
   the state machine; nothing transitions into them. CAB buyers require
   requester ≠ approver, and gate #4 below fails on process grounds without it.
5. **Runner lifecycle gaps:** no revocation/rotation endpoint (the 401 message
   says "revoked" aspirationally), no job lease recovery (a runner dying
   mid-job leaves it `running` forever), join-token minting is open when auth
   is off and no admin token is set.
6. **Tenancy is DB-row-level only.** Intents, rendered configs, reports, and
   the git workspace are one shared filesystem across orgs. The beta is
   therefore **single-tenant-per-deployment**, declared openly, until per-org
   workspaces exist.

**Missing entirely:** installer/onboarding a stranger's *security team* can
approve, pricing page, demo assets, design partners, evidence-bundle export +
external verification, legal/insurance pack, SOC 2 motion, support story.

## Launch definition and go/no-go gates

"Launch" = **paying design partners (paid pilots) with at least one
production-grade change proven per partner, referenceable.** Full production
evidence-acceptance is a follow-on gate — CAB and procurement calendars make
it a month-4-to-6 outcome, and pretending otherwise sets a kill criterion that
triggers for calendar reasons.

Gates:

1. **The stranger test** (M1 exit): a network engineer who has never seen
   Netcode reads one exported evidence record and answers, unaided: what was
   requested, which devices, exact commands, config touched?, rollback ready?,
   validation/dry-run/verify results, approver identity, evidence complete?
2. **The 10x gate** (M2 exit): the golden-path suite runs green 10 consecutive
   times against the **release lab** — a dedicated small (3-node) containerlab
   on our own hardware, run nightly + pre-release. Cloud CI runs the mocked
   suite; the hardware gate is scheduled and pre-release. (cEOS images are
   license-gated and RAM-heavy: a hosted-CI lab is not feasible; we accept and
   document the self-hosted release-lab SPOF for beta.)
3. **The 30-minutes-after-prerequisites test** (M2 exit): once the security
   pre-flight is approved (runner host exists, egress rule open, service
   account issued), a stranger goes from installer → enrolled runner → first
   proven change in under 30 minutes using only the quickstart. We publish the
   pre-flight pack precisely because the *real* clock at a partner is 2–8
   weeks of their process — we make that path paved, not pretend it away.
4. **The evidence-acceptance gate** (post-launch, month 4–6): 2+ partners get
   a Netcode evidence record accepted by their change process (CAB, auditor,
   or manager-of-record). The PMF signal that matters.
5. **Zero Netcode-caused incidents**, counted publicly, forever. One bad apply
   at a partner = full-stop postmortem + safety-spine freeze.

## Workstream 0 — Pay the trust debt (week 1, before anything else)

- Purge discovery credentials from persisted job payloads (redact on claim,
  never return via `/api/jobs`, short TTL); document that discovery of a
  not-yet-trusted device is the one moment creds transit — encrypted, never
  stored.
- Runner prefers its **local** `POLICY_FILE` over payload policy; ship
  non-empty fail-closed defaults for `custom_config` (credential/AAA/SNMP
  fragments always blocked even with no policy present).
- Approval gate v0: an approve endpoint + UI using the existing `approved`
  state, **requester ≠ approver**, approver identity recorded in the evidence
  record. External change-ticket reference field (free text + URL) on every
  change, surfaced in the evidence record — ServiceNow/Jira *integration* is
  post-launch; the reference field is week 1.
- Job lease recovery (claimed jobs time out and requeue), runner
  revoke/rotate endpoints, join-token minting locked behind admin auth always.
- Mark user-supplied `custom_config` rollback as **"rollback: user-supplied,
  unverified"** in the evidence record unless it passed its own dry-run. An
  auditor-sealed wrong rollback is worse than a screenshot.
- Commit the live-proof artifacts (drift-sabotage, troubleshoot, golden-path
  e2e reports) to `reports/` so "proven live" is evidenced in-repo.

## Workstream 1 — The Safe-Change Cockpit (UI)

Reframe every screen from "did the job finish?" to "is it safe to continue?"
Five signature patterns, systemwide; seeds exist, this is elevation:

1. **Persistent Live Outcome rail** — the existing outcome panel
   (action/expected/actual/artifact/device/next in `static/app.js`) becomes
   persistent and identical on every view. Never show a result without
   expected-vs-actual.
2. **Device-touch chip** — promote the existing strings (21 call sites) into
   one first-class chip: `Not touched` / `Candidate only` / `Will be touched
   after approval` / `Touched at <time> by <who>`.
3. **Exact command + rollback preview as the apply gate** — Apply lives
   physically below the forward + rollback commands, disabled until dry-run
   proof exists *and* approval is granted; the UI says why.
4. **Expected-vs-actual proof table** — one component
   (`Check | Expected | Actual | Result | Evidence`) for validation, dry-run,
   verify, drift, investigation.
5. **Evidence completeness score** — `N/9` chip from the manifest (request,
   intent, plan, rollback, validation, dry-run proof, approval, apply
   transcript, verify proof). Incomplete = warning state.

Noun/status sweep: no "Job" as a primary noun; raw JSON behind "Show raw
output"; statuses stay lifecycle-truthful (already are). Dark, serious
treatment for the high-stakes surfaces (command preview, apply gate, evidence).

## Workstream 2 — Signed evidence, literally

- SHA-256 content hash per artifact in the manifest (`manifest_entry`).
- **Ed25519 at launch**: runner generates a keypair at enrollment, private key
  never leaves the runner, public key registered with the control plane;
  manifest signed on the runner at apply/verify. (HMAC stays for job-result
  transit.) This is what lets a third party verify without trusting us.
- Export bundle: `CHG-<id>.zip` — record, artifacts, transcripts, manifest,
  signature. `netcode verify-evidence <bundle>` + a Verify button. Demo line:
  tamper one byte → verification fails, *against a key we provably don't hold*.
- Evidence retention answer for the auditor: bundles are exportable and
  verifiable offline forever — a customer's evidence outlives us. Say so.

## Workstream 3 — Production hardening

- **Postgres live** — including data migration from the running SQLite
  deployment, connection pooling (the runner poll loop opens a connection per
  poll today), and the full suite green against `DATABASE_URL`.
- **Auth on for hosted** — plus the missing user-management surface (invite,
  org bootstrap), session hardening (move token out of localStorage, login
  rate-limiting, session purge), org-scoping audit of every route
  (`/api/audit/sessions`, `/api/workflow/change/{id}`, and path-taking
  endpoints that currently read arbitrary filesystem paths).
- **Runner as a supervised service** — hardened container image (podman/
  docker) as the primary packaging (a partner's security team approves an
  image with a digest, not `pip install` on a random VM), systemd unit
  alternative, watchdog alerting on heartbeat gaps, documented **mid-apply
  failure semantics**: what state the device is left in if the runner dies
  mid-session (EOS config-session aborts on disconnect — say it, prove it),
  and a sanctioned manual-fallback path that doesn't poison drift.
- **Credentials at rest**: encrypted runner inventory v0 (age/fernet keyed by
  a host secret), Vault/CyberArk read-through as fast-follow; document TACACS+
  service-account provisioning and exact device prerequisites (SSH; eAPI not
  required — say so explicitly, it's an onboarding objection).
- Long-poll capacity note: bounded worker pool for runner polls so a handful
  of runners can't starve the UI threads.
- Release-gate CI as defined in gate #2. Backups + restore drill for the
  store; structured audit log retention.

## Workstream 4 — Packaging and onboarding

- **Security pre-flight pack** (the document their security team says yes to,
  distinct from the marketing whitepaper): exact egress destinations (one
  line), data-classification table — *what stays on the runner (credentials,
  live sessions) vs what reaches the cloud (intents, diffs, transcripts,
  signed results)* — runner packaging + patch cadence, credential flows,
  device prerequisites, SIG-Lite/CAIQ answers pre-filled.
- Container-first install + enroll; 15-minute quickstart; the
  30-minutes-after-prerequisites test runs against it.
- **Recorded demo is primary** (professionally recorded once: golden path →
  out-of-band sabotage → drift names the violated change → rollback → export
  signed evidence → tamper → verification fails — *plus one genuinely scary
  change*: an ACL edit on a PCI boundary via `custom_config` with verified
  rollback, because a VLAN add alone doesn't transfer trust to the changes
  people actually fear). Live demo only after the demo environment has run
  green 7 consecutive days.
- **Shadow mode is the named adoption motion for skeptics**: free **60-day
  full-estate read-only trial** (drift + investigate across everything,
  no write path) → graduate one change type. Free-forever tier stays capped
  (~25 devices); the time-boxed full-estate trial is what hooks a 300-device
  shop. Read costs us little; trust compounds from watching it be right about
  their network for a month.

## Workstream 5 — GTM (starts week 1, in parallel — not after the build)

- **Positioning (verbatim):** "Before any change touches a device, Netcode
  shows the exact command, exact rollback, safety verdict, and dry-run proof.
  After the change, it verifies live state and signs the evidence.
  Credentials never leave your network." Every word now literally true
  (Workstream 0/2 make it so).
- **Two pitches, one product**: engineer (champion — dry-run proof, rollback,
  device-touch honesty) and manager/compliance (buyer — audit cost, incident
  cost, evidence acceptance). The pricing conversation happens with the buyer;
  WTP signal from engineers is noise.
- **ICP honesty on the pricing page**: Arista-write today, 11-vendor
  read/drift/investigate for everyone; a majority-Cisco shop is a shadow-mode
  + read customer until IOS-XE write ships. Say it before they burn a
  security-review cycle discovering it.
- **Funnel with honest math**: 400+ account list (Arista communities, NetBox
  community, NAF/AutoCon, job postings mentioning EOS + compliance), built as
  real allocated work. Discovery conversations from week 1 (they don't need
  the finished product). Content trail + AutoCon CFP submitted early — for a
  no-brand solo founder, community warmth is the only thing that makes the
  meeting math close.
- **Design partner offer**: **flat paid pilot — $5k for 6 months, credited
  toward the annual contract** — instead of discounted per-device (cleaner WTP
  signal, one PO line, no $1.5k ACV trap). Case-study/logo/quote rights in the
  pilot agreement as the explicit trade. Target: 3–5 signed pilots.
- **Legal/insurance workstream** (always bites first-timers): pilot agreement
  template with limitation-of-liability + consequential-damages exclusion,
  DPA, E&O/cyber insurance certificates ready before the first redline asks.
- **Support boundary, written down**: business-hours support + best-effort
  change-window standby *scheduled in advance* for pilot partners; runner
  failure semantics documented (Workstream 3) so 2am doesn't require us awake.
- Security pack: pre-flight doc + whitepaper + threat model; SOC 2 (Vanta-
  class) starts now; launch gates on documents + architecture, not the cert.
- Pricing page early: pilot offer + $30–80/device/yr post-pilot bands + free
  tier + 60-day trial. Test with buyers, not champions.

## What we will NOT build for launch (discipline list)

- Cisco IOS-XE write — not until 3+ paying Arista pilots validate WTP. The
  11-vendor read path is the on-ramp and the shadow-mode trial.
- OS-upgrade change type + canary batches (`scale.py:rollout_plan` is the
  seed) — the flagship fast-follow, not launch.
- ServiceNow/Jira *deep* integration — the ticket-reference field ships week 1;
  the webhook that attaches evidence bundles to a SNOW change is the first
  post-launch integration, driven by partner demand.
- MSP multi-tenant edition; white-label evidence — phase 3.
- Generic troubleshooting platform — Investigate stays subordinate to changes.
- Source-of-truth ambitions — NetBox is a channel; deepen the read
  integration if NetBox Assurance moves toward execution.
- AI-branded anything. The wedge is trust.

## Timeline (16 weeks to launch; evidence-acceptance gate follows)

**Weeks 1–4 — M1: Trust debt + Cockpit + Signed evidence.**
Workstream 0 complete (security fixes, approval gate, ticket field); five
signature patterns systemwide; hashed + Ed25519-signed manifest; export +
verify. GTM: account list building, discovery calls begin, AutoCon CFP.
*Exit:* stranger test passes; tamper demo works against asymmetric keys;
Codex adversarial walkthrough rates trust ≥4/5.

**Weeks 4–10 — M2: Production-grade + packaged.** (Honest sizing: the code
audit sized the original 3-week version at 8–10 weeks of work; this is where
the schedule absorbs it.) Postgres with migration; auth on + user management +
route audit; supervised containerized runner with failure semantics; encrypted
creds at rest; pre-flight pack; quickstart; recorded demo; pricing page;
release-lab CI gate. GTM: 20+ discovery conversations held, pilot agreement +
insurance ready. *Exit:* 10x gate green; 30-minutes-after-prerequisites test
passes with a stranger; demo environment green 7 days.

**Weeks 10–16 — M3: Paid pilots = launch.** Convert discovery pipeline to
**3–5 signed $5k pilots**; each runs ≥1 production-grade change (their lab or
low-risk prod change per their process) with a signed evidence record; weekly
iteration. *Exit / launch:* 3+ paid pilots active, each with a proven change
and an exported evidence bundle in their hands, zero incidents. Public
launch: pricing live, free tier + trial open, demo video, launch post.

**Months 4–6 — the follow-on gate:** 2+ partners get an evidence record
accepted by their CAB/auditor (their calendar, not ours), and answer yes,
verbatim: **"I would use this for my change window."** IOS-XE write decision
made on this data.

## Risks and kill criteria (time-boxed on the input side)

- **By week 16: fewer than 20 qualified conversations held OR fewer than 2
  signed pilots** → stop building, diagnose which (pipeline vs product),
  revisit segment (PCI retail needs IOS-XE; MSPs need multi-tenancy) before
  writing more code. The input-side clause exists so the criterion can't be
  dodged by simply not doing outbound.
- **Pilots engage but won't pay $5k** → the wedge isn't valued at manager
  level; re-test as free-trial-to-annual with two partners max, 4 weeks, then
  decide. (Named honestly: PLG barely exists for on-prem-runner products —
  this branch is a diagnosis, not a strategy.)
- **One bad apply at a partner** → full stop, postmortem to the partner,
  safety-spine freeze until root-caused. Existential, and also why we exist.
- **CloudVision objection kills >30% of conversations** → accelerate IOS-XE
  and multi-vendor read messaging; our counters are git-native workflow,
  signed portable evidence, multi-vendor read, runner trust story.
- **NetBox Assurance ships execution** → deepen integration (Netcode as the
  execution/evidence rail for NetBox intent) before they build one.
- **Founder bandwidth** — M3 is 15–20 hrs/wk of list work, calls, security
  questionnaires, and redlines on top of support. The build freezes for M3
  except partner-blocking fixes; that is a scheduling decision made now.

## Metrics that matter

- Time-to-first-proven-change *after prerequisites* (<30 min), and
  time-through-prerequisites per partner (measure the real clock too).
- Evidence records per partner per week; % with 9/9 completeness.
- Drift catches per partner (each one is a retention event).
- Pilot conversion to annual; the verbatim monthly question: *"Would you use
  this for your change window?"* — and whether they actually did.
- Zero Netcode-caused incidents, counted publicly, forever.

## Roles

- **Syed** — discovery calls from week 1, demos, pricing, pilot agreements,
  final scope calls.
- **Claude** — implements; every feature live-proven on the lab before it
  ships (built → tested → live-proven → committed), proof artifacts in-repo.
- **Codex** — validates every milestone exit; adversarial persona walkthroughs;
  the M1 trust rating.

---
*Review provenance: v2 incorporates a three-critic adversarial pass —
skeptical buyer (CAB/security-review realism, support story, credential
custody), GTM operator (funnel math, paid-pilot pricing, legal/insurance,
build-then-sell sequencing), staff-engineer code audit (trust-debt findings
with file references, milestone sizing, CI-lab feasibility). All three:
"fix, then ship."*
