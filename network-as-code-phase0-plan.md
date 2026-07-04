# Phase 0 — SaaS Control Plane + On-Prem Runner (Mac + ORB Lab Test)

Date: 2026-07-03. Executes Phase 0 of `network-as-code-saas-launch-plan.md`.
Goal: prove the SaaS shape end-to-end on real hardware you own — control plane
as "SaaS" on the Mac, lightweight runner as "on-prem collector" next to the
Arista lab, full Story-3 change flow through the queue, with the safety spine
intact and credentials never leaving the runner.

## Test topology (verified by probes, 2026-07-03)

```text
┌──────────────── macOS (the "SaaS") ─────────────────┐
│  Control plane: netcode API + UI    http://:8088    │
│  - stories, gates, plan, validation, workflow,      │
│    evidence, git, job QUEUE, runner registry        │
│  - NO device credentials, NO device reach (verified:│
│    172.100.1.41 unreachable from Mac)               │
└───────────────▲─────────────────────────────────────┘
                │ outbound-only HTTPS/long-poll (runner dials out)
                │ VM → Mac via host.orb.internal / 192.168.139.1  (verified)
┌───────────────┴──── ORB VM "clab" (the "on-prem") ──┐
│  Runner: netcode-runner (pool: store-lab)           │
│  - holds device credentials LOCALLY                 │
│  - re-runs fail-closed policy checks locally        │
│  - executes EOS config sessions (same adapter code) │
│  - signs evidence, uploads outbound                 │
│  → devices v2-store1/2/3 (172.100.1.41-43)          │
└─────────────────────────────────────────────────────┘
Optional: second runner ON the Mac in pool "mac-local" to demo multi-runner
pools and honest no-device-reach behavior.
```

The Mac-can't-reach-lab fact is a feature: the e2e test *structurally proves*
"browser/SaaS never touches devices" — if the control plane could cheat, the
test would still pass; here it cannot cheat.

## Architecture

### Execution modes
`NETCODE_EXECUTION=local|runner` (control-plane setting, default `local`).
- `local`: today's behavior, unchanged — all 37 tests and the current ORB
  deployment keep working. The safety spine is never forked.
- `runner`: `JobRunner` (the seam built for exactly this) gate-checks the
  workflow as today, then **enqueues** the job instead of executing it. The
  runner claims, executes, and reports.

### Job flow (runner mode)
1. UI → `POST /api/lab/dry-run` (unchanged endpoint)
2. Control plane: `require_action_allowed` (gate stays cloud-side) → create
   job `status=queued` with payload: `{action, device_id, device_host,
   platform, intent_yaml, rendered_config, policy_yaml, change_id}` —
   **never credentials**
3. Runner long-polls `POST /api/runner/poll` → claims job (single claimant,
   atomic)
4. Runner re-validates locally (fail-closed): render-scope + blocked
   fragments + intent policy against its LOCAL policy file — a compromised
   control plane cannot push forbidden config
5. Runner resolves credentials from its LOCAL store by device id/host,
   executes via the existing `AristaEOSLabAdapter` (identical session-abort
   discipline), collects evidence
6. Runner signs the result (Phase 0: HMAC-SHA256 with its enrollment secret;
   real asymmetric signing in Phase 1) → `POST /api/runner/jobs/{id}/result`
7. Control plane verifies signature, updates job/change/workflow state,
   records events — UI polls the job to completion and renders outcomes
   exactly as today

### Runner protocol (all runner-initiated; control plane never dials in)
| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /api/runners/join-token` | admin | mint single-use join token scoped to a pool |
| `POST /api/runner/enroll` | join token | `{name, pool}` → `{runner_id, runner_token}`; token stored hashed |
| `POST /api/runner/poll` | runner token | long-poll (~25s hold) → job payload or 204 |
| `POST /api/runner/jobs/{id}/result` | runner token | signed result + evidence upload |
| `POST /api/runner/heartbeat` | runner token | liveness + version |
| `GET /api/runners` | admin/UI | list runners: pool, status, last_seen |

### Credential custody (Phase 0 rules)
- Control-plane inventory: device metadata only (id/host/platform/site);
  password fields stripped in runner mode.
- Runner keeps `~/.netcode-runner/inventory.yaml` (full, with creds) +
  `policy.yaml` (local policy copy) + `identity.json` (runner id + token).
- CI-enforced invariant test: no job payload, job record, evidence record, or
  API response in runner mode contains a password value.

### What does NOT change in Phase 0
The safety spine: 7 fail-closed checks, dry-run-gated apply, rollback
discipline, evidence records, git flow. Postgres and full auth/RBAC are
Phase-0-late (M5) — SQLite + single admin token suffice for the Mac test and
don't touch the demo path.

## Milestones

### M1 — Runner protocol + queue (backend)
Store: `runners` + `join_tokens` tables; jobs gain `payload/claimed_by/signature`
columns (via existing `_ensure_column` migration pattern). New
`netcode/runner_hub.py`: mint/enroll/claim/submit/heartbeat/list. API endpoints
above. `NETCODE_EXECUTION` switch in `JobRunner`.
**Exit:** hermetic tests — enroll happy/replay/bad-token, queue→claim→submit
updates change+workflow, payload contains no credentials, local mode untouched
(all prior tests green).

### M2 — Runner agent
New `netcode/runner_agent.py` + console script `netcode-runner` (stdlib
`urllib`, no new deps): `enroll` and `run` commands, poll loop, local
fail-closed re-check, local credential resolution, execution via existing lab
adapter, HMAC-signed results, graceful SIGTERM drain (finish in-flight job).
**Exit:** on the Mac, control plane in runner mode + runner process on the
same Mac against a FAKE unreachable device → job flows queue→claim→honest
device-unreachable failure with evidence. Signature verified.

### M3 — UI async jobs + Runners panel
Lab actions in the UI poll the job to a terminal state (same outcome panels).
Setup gains a Runners card: mint join token, list runners with pool/last-seen,
and Gate-4 proof mode reflects runner connectivity in runner mode.
**Exit:** clicking dry-run in runner mode shows queued→running→pass with the
same evidence rendering as local mode.

### M4 — The e2e demo (the point of Phase 0)
Deploy: control plane on Mac (`uvicorn 0.0.0.0:8088`, runner mode); runner
enrolled on clab VM (`pool=store-lab`, creds local); UI from Mac browser.
Run full Story 3: declare VLAN → branch → plan → validate → dry-run (queued →
executed on VM → proof back) → commit → apply → verify → push → evidence
record → rollback.
Negative proofs: runner stopped → job queues honestly + UI says so; apply
before dry-run → blocked cloud-side; `username ...` config → blocked at BOTH
layers (kill cloud check in a test build to prove the runner check alone
blocks); grep all cloud-side artifacts for credential strings → zero.
**Exit:** the full golden path + 4 negative proofs pass with control plane and
runner on different machines.

### ✅ M1–M2 + M4 DONE (2026-07-03) — proven end-to-end on real hardware

M1 (runner protocol + queue) and M2 (runner agent) shipped and unit-tested
(39 tests). M4 e2e ran live: **Mac control plane in runner mode (device
password stripped from its inventory) + on-prem runner on the ORB clab VM
(credentials local) drove a full change against the real Arista device
`v2-store1`:**

- Mac `/api/health` showed lab UNREACHABLE from the Mac; ORB VM reached the
  Mac control plane on `:8095` (HTTP 200) — the SaaS/on-prem asymmetry, real.
- Runner enrolled via single-use join token (two-phase), registered `online`.
- Plan (Mac, no device) → dry-run **queued** → runner claimed, ran the EOS
  config-session diff on the real device, aborted it, HMAC-signed the result;
  control plane verified the signature → `dry_run_passed`.
- Commit (git) → apply **queued** → runner committed VLAN 94 on the device →
  `rollback_available`. **Independent SSH (bypassing netcode) confirmed
  `VLAN 94 PHASE0_RUNNER active`.**
- Rollback **queued** → runner removed it → `rolled_back`; independent SSH
  confirmed `VLAN 94 not found`. Lab left clean.
- Credential-custody proof: queued job payloads carried NO password (asserted
  in test + live); the Mac inventory had no device password at all, yet the
  change succeeded because the runner supplied creds locally.
- Negative proof: with the runner stopped, a queued dry-run stayed `queued`,
  the change stayed `validated`, and VLAN 95 never appeared on the device —
  the control plane structurally cannot self-execute.

### ✅ M3 DONE (2026-07-03)

The browser now drives the runner. `awaitLabResult()` detects a queued
(runner-mode) response and polls `GET /api/jobs/{id}` to a terminal state,
normalizing it into the exact shape local mode returns — so dry-run/apply/
rollback render identically whether executed in-process or on the runner, with
a live "on runner…" progress state. Setup gained a **Runners panel** (mint
single-use join token + enroll command, live runner list with online/offline
chips), the Proof-mode gate is runner-aware (Runner-backed / No runner online),
and standalone Verify is disabled in runner mode (apply already verifies on the
runner). `/api/health` exposes `execution.mode`. Verified: control plane on Mac
(mvp11, runner mode) served the panel, runner online, and the browser polling
path ran `queued → running → completed|pass` with full transcript; lab left
clean (VLANs 93–96 all absent).

Remaining in Phase 0: **M5** (Postgres, auth/RBAC/multi-tenancy, change-type
registry refactor, security whitepaper + failure-domain doc). Also deferred:
routing read paths (verify/readiness/drift/discovery) through the runner — today
they run control-plane-side and so are local-mode only.

### ✅ M5 (mostly) DONE (2026-07-03) — SaaS backbone: auth, RBAC, multi-tenancy, Postgres-readiness

Implemented against an adversarial design spec. **40 tests pass** (was 39).

- **Multi-tenancy:** `orgs`/`users`/`sessions` tables + `org_id` on
  changes/jobs/runners/join_tokens (via `_ensure_column`, backfilled to
  `org_default`). Every change/job/runner is tenant-scoped; a runner may only
  claim jobs in its own org (colliding pool names across tenants stay isolated);
  cross-tenant single-record reads return **404** (no existence leak).
- **Auth:** `netcode/auth.py` — pbkdf2-sha256 password hashing (600k iters,
  per-user salt, `compare_digest`), opaque `nut_` session tokens (sha256-stored,
  12h TTL). Three token namespaces coexist (`njt_` join / `nrt_` runner / `nut_`
  user), each with its own lookup — a user token can't act as a runner and
  vice-versa.
- **RBAC:** roles admin/operator/viewer enforced by middleware — reads need
  viewer, writes need operator, join-token minting needs admin. `POST
  /api/auth/login|logout`, `GET /api/auth/me`. Env-driven idempotent bootstrap
  admin so flipping the flag never locks anyone out; legacy `NETCODE_ADMIN_TOKEN`
  stays as break-glass.
- **Gating:** all of the above is behind `NETCODE_AUTH` (default **off**). Off =
  every request is a system admin on `org_default`, so the current UI and every
  test keep working byte-for-byte. Live-verified both ways: off → no login,
  `/api/changes` open; on → 401 without token, login works, authed → 200.
- **Postgres-readiness:** `store.py` is `DATABASE_URL`-driven (sqlite default,
  `postgresql://` via psycopg); a connection wrapper rewrites `?`→`%s`, pragmas
  are sqlite-gated, `_ensure_column` has an information_schema branch. **Validated
  on SQLite** (incl. a live run against `DATABASE_URL=sqlite:////tmp/...`); the
  psycopg path is structured but needs a live Postgres + `FOR UPDATE SKIP LOCKED`
  claim to validate — deferred.
- **UI:** login overlay shown only when auth is on and unauthenticated; Bearer
  token attached to all fetches; viewer role hides write actions (server enforces
  regardless). Asset `mvp12`.
- **Docs:** `docs/SECURITY_WHITEPAPER.md` and `docs/FAILURE_DOMAINS.md` shipped.

**Remaining M5 hardening (follow-ups, tracked honestly):** per-endpoint
change-ownership checks on every mutating-by-`change_id` endpoint (dry-run/apply/
rollback/verify/drift) — today list + record + the primary create/read paths are
scoped, but exhaustive per-endpoint ownership on all mutating routes is the next
pass; live Postgres validation; org/user management endpoints (users seeded via
store today); asymmetric runner result signing (HMAC today).

### M5 — SaaS-able hardening (original notes)
Postgres migration (SQLAlchemy or thin driver swap), minimal login (single
org, admin/operator roles), change-type registry refactor (pay before Cisco),
security whitepaper + failure-domain doc drafts.

## Demo script (M4 acceptance)
```bash
# Mac — control plane ("SaaS")
NETCODE_EXECUTION=runner NETCODE_ADMIN_TOKEN=<secret> \
  uvicorn netcode.api:app --host 0.0.0.0 --port 8088
# Mac — mint a join token (UI Setup → Runners, or curl)
# clab VM — runner ("on-prem collector")
netcode-runner enroll --server http://host.orb.internal:8088 \
  --join-token <token> --pool store-lab --name clab-runner-1
netcode-runner run
# Browser on Mac → http://127.0.0.1:8088/app → run Story 3 end to end
```

## Risks / watch items
- VM→Mac connectivity is IPv6 (`host.orb.internal`) or via gateway
  `192.168.139.1`; bind uvicorn appropriately and verify at M4 start.
- Long-poll through OrbStack NAT: keep hold ≤25s with jittered re-poll.
- SQLite under concurrent poll writes: single-runner Phase 0 is fine; WAL
  mode + busy_timeout added in M1 to be safe (also fixes a known gap).
- UI double-path (sync local / async runner): keep one rendering path by
  normalizing local results into the same job envelope.
