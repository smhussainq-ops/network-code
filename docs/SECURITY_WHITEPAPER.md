# Netcode Security Whitepaper

> **Historical baseline plus current delta:** The body below preserves the Phase 0 trust-boundary design. Connector leases, uncertain-write reconciliation, organization scoping, bounded bearer credentials, rotation, revocation, drain, and queue controls shipped after the original text. Use `docs/R5_CONNECTOR_RELIABILITY_EVIDENCE_2026-07-14.md` and the current code for the launch control inventory; do not rely on the old Phase 0 "not yet" ledger where it conflicts.

## SaaS Control Plane + On-Prem Runner Architecture

**Document status:** Phase 0 trust-boundary record with a 2026-07-14 control delta. The dated R5 and R12 evidence documents define current launch behavior.
**Audience:** Security reviewers, CISOs, and CABs evaluating Netcode for a mid-market network estate.
**Scope:** This document describes the original trust architecture of the Netcode control plane and the on-prem runner. File and line references in the historical body may have moved; launch claims must be checked against the dated evidence documents and current code.

---

## 1. Executive summary

Netcode is a multi-vendor network-operations platform with an evidence-bound capability matrix. Its currently proven write surface is intentionally narrower than its read surface. It splits into two trust domains:

- A **SaaS control plane** (FastAPI, `netcode/api.py`) that hosts the workflow, the change record, the job queue, and the evidence store. It runs in Netcode's cloud.
- An **on-prem runner** (`netcode/runner_agent.py`) that the customer runs inside their own network, next to the devices. It holds device credentials, executes changes, and reports signed results.

The security posture rests on a single structural claim, which the rest of this document substantiates against code:

> **Device credentials never transit the cloud, and the control plane cannot make a runner push forbidden configuration — not by policy, but by construction.**

Two independent facts establish this. First, the job payload the control plane builds and queues is deliberately credential-free (`netcode/jobs.py::_runner_payload`); the runner resolves credentials from its own local inventory by device id (`netcode/runner_agent.py::_execute_job`). Second, before touching any device the runner re-runs a fail-closed policy gate locally against the exact config it is about to push (`netcode/runner_checks.py::local_policy_gate`), so a compromised or buggy control plane still cannot cause a forbidden change.

This has been proven on hardware: with `NETCODE_EXECUTION=runner`, the queued job payload carried no password, the control-plane inventory had the device password stripped, and configuration changes still succeeded — because the runner supplied credentials from its own local store. Current regression counts are preserved in the dated evidence documents rather than embedded here.

We are explicit about what is *not* yet done. Result integrity today uses **HMAC-SHA256 with a per-runner shared secret**, not asymmetric signing. Local/community state defaults to SQLite, while hosted deployment supports Postgres and still requires public backup/restore proof. Section 10 preserves the original ledger plus the current identity and storage delta.

---

## 2. Architecture and trust boundaries

```
   Customer network (trusted)                 |   Netcode cloud (semi-trusted)
                                              |
  ┌──────────────────────────┐   outbound     |   ┌────────────────────────────┐
  │  On-prem runner          │   HTTPS only    |   │  Control plane (FastAPI)    │
  │  netcode/runner_agent.py │ ─────────────► |   │  netcode/api.py             │
  │                          │                 |   │  netcode/runner_hub.py      │
  │  • device credentials    │                 |   │                            │
  │    (local inventory)     │ ◄───────────── |   │  • job queue, workflow      │
  │  • local policy gate     │   job payloads  |   │  • change + evidence store  │
  │  • EOS config sessions   │   (no creds)    |   │  • signature verification   │
  │  • HMAC result signing   │                 |   │  netcode/store.py (SQLite)  │
  └──────────────────────────┘                 |   └────────────────────────────┘
        devices (SSH/eAPI)                     |
```

Two trust domains, one direction of connection. The trust boundary is the customer's network edge. Everything inside it — credentials, live device sessions, config rendering — stays inside it. The cloud holds the *record* of what happened, signed at source, but never the means to reach a device.

The control plane is treated as **semi-trusted**: useful, authenticated, but explicitly assumed potentially-compromised in the threat model (Section 8). The runner does not trust it.

---

## 3. Outbound-only connection model and egress allowlist

The runner is a pure outbound client. It never binds a listening socket, and the control plane never dials it. All communication is initiated by the runner over HTTP(S) using the Python standard library `urllib` (`runner_agent.py` lines 51–65). There is no inbound attack surface on the runner: no open port, no webhook receiver, no daemon accepting connections.

Job delivery uses **long-polling** rather than a push channel. The runner posts to `/api/runner/poll` with a `wait_seconds` value; the control plane holds the request open until a job is available or the deadline passes, capped server-side at 25 seconds (`runner_hub.py::poll_for_job`, line 76) and client-side at a 40-second socket timeout (`runner_agent.py`, line 183). A `204 No Content` means "no job — poll again."

### Exact egress allowlist

The runner communicates only with the configured control-plane origin. The original Phase 0 calls remain below; current releases add same-origin lease renewal, credential rotation/confirmation, and the authenticated interactive Shell channel. No inbound listener is opened on the runner:

| Method | Path | Purpose | Auth |
|---|---|---|---|
| POST | `/api/runner/enroll` | Two-phase enrollment (one-time) | Single-use join token in body |
| POST | `/api/runner/heartbeat` | Liveness + version report | Bearer runner token |
| POST | `/api/runner/poll` | Long-poll for the next job | Bearer runner token |
| POST | `/api/runner/jobs/{job_id}/result` | Upload signed result | Bearer runner token |

(The `enroll` call is made once by the `enroll` subcommand; `heartbeat`, `poll`, and `result` are the steady-state calls in `run()`.)

A network reviewer can therefore write a firewall egress rule of the form: **allow the runner host outbound HTTPS to `<control-plane-host>` only**, plus whatever SSH/eAPI reach it needs to the managed devices. Nothing else is required. This mirrors the well-understood egress posture of HashiCorp Cloud agents, GitHub self-hosted runners, and Teleport.

**Phase 0 transport honesty:** the enrollment default in the CLI help shows an `http://` example for local/lab bring-up, and the agent will speak plain HTTP if pointed at an `http://` URL. In any real deployment the `--server` URL is `https://`, and TLS termination/pinning is the operator's responsibility today. Enforced TLS and certificate pinning at the agent are **Planned** (Section 9).

---

## 4. Two-phase enrollment and runner identity

Enrollment separates *bootstrapping trust* from *operating identity*, so that the long-lived credential never travels as a bearer token in day-to-day traffic and the bootstrap secret is single-use.

**Phase one — mint a single-use join token.** An operator calls `/api/runners/join-token` (admin-guarded, Section 7) to mint a token scoped to a runner pool (`runner_hub.py::mint_join_token`). The token is `njt_` + 32 bytes of `secrets.token_urlsafe` entropy. It is shown once. Critically, **only the SHA-256 hash of the token is stored** (`_hash`, line 25; `store.create_join_token`) — the plaintext exists only in the operator's hands.

**Phase two — enroll and receive operating identity.** The runner posts the join token to `/api/runner/enroll` (`runner_agent.py::enroll`). The control plane consumes the token atomically and issues a durable identity (`runner_hub.py::enroll_runner`):

- a **runner token** (`nrt_` + 32 bytes entropy) — the bearer credential for all subsequent calls, of which again **only the SHA-256 hash is persisted** (`store.create_runner`);
- an **HMAC secret** (32 bytes entropy) used to sign results.

The runner writes these to `~/.netcode-runner/identity.json` and **chmods the file to `0600`** (`runner_agent.py`, lines 82–83), so the operating identity is owner-readable only.

**Single-use enforcement is atomic and replay-safe.** `store.consume_join_token` performs `UPDATE join_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL` and treats `rowcount != 1` as failure (lines 367–377). A replayed or already-used token cannot enroll a second runner — the conditional UPDATE is the concurrency guard, not a read-then-write race.

**Authentication of steady-state calls.** Every `poll`, `heartbeat`, and `result` call carries `Authorization: Bearer <runner-token>`. The control plane hashes the presented token and looks up the runner by hash (`runner_hub.py::authenticate_runner` → `store.runner_by_token_hash`). No plaintext token is ever compared or stored.

**Identity scoping.** A runner belongs to exactly one pool. It can only claim jobs queued for its own pool (`poll_for_job` passes `runner.pool` to `claim_next_job`), and job claiming is atomic across concurrent runners (`store.claim_next_job`, conditional UPDATE, lines 432–447). This makes pool-per-tenant isolation a natural Phase 1 primitive.

**Current identity boundary:** the connector token remains a bearer secret rather than an asymmetric key or mTLS certificate. Tokens are hash-stored, time-bounded, rotated through a prepare/confirm protocol with bounded overlap, and can be revoked per connector. Revocation also requests drain and prevents further authentication. Asymmetric runner-held identity remains planned.

---

## 5. Credential custody — why device credentials structurally cannot reach the cloud

This is the load-bearing security property. It is enforced in two places in the code, on both sides of the boundary.

**On the cloud side, the job payload is built credential-free.** When a lab action is queued for a runner, the control plane assembles the job spec in `netcode/jobs.py::_runner_payload` (lines 144–166). The device sub-object it ships contains **only** `id`, `host`, `platform`, and `port`:

```python
"device": {
    "id": device.id,
    "host": device.host,
    "platform": device.platform,
    "port": device.port,
},
```

There is no `username`, no `password`, no key material in the payload — by construction, not by redaction. The docstring states the intent explicitly: *"Deliberately credential-free: the runner resolves credentials from its own local store by device id."*

**On the runner side, credentials come only from local inventory.** In `runner_agent.py::_execute_job` (lines 133–143), the runner ignores any credential-shaped data in the payload and resolves the device from its own `~/.netcode-runner/inventory.yaml` by device id:

```python
# Credentials come ONLY from the runner's local inventory, never from the cloud payload.
if not INVENTORY_FILE.exists():
    return {... "message": f"No local inventory at {INVENTORY_FILE}; cannot resolve credentials."}
inventory = Inventory(INVENTORY_FILE)
device = inventory.by_id.get(device_id)
```

The `AristaEOSLabAdapter` is then constructed from that locally-resolved `device` object. The cloud passes an *identifier*; the runner supplies the *secret*. The two never meet in the cloud.

**Proven, not asserted.** This property has been demonstrated on real hardware: the queued payload carried no password, the control-plane's own inventory had the device password stripped out, and the configuration change still applied successfully — because the runner never needed the cloud to know the password. If the mechanism were cosmetic, that test would have failed at connect time. It did not.

**What this means for a reviewer:** a breach of the Netcode cloud — database exfiltration, malicious insider, supply-chain compromise of the control plane — yields device *hostnames and ports*, and the change *history*, but **no device credentials**, because they were never present to steal.

**Phase 0 custody honesty:** the runner stores device credentials in a local `inventory.yaml` on disk. This keeps them inside the customer's trust boundary, which is the essential property. It is not yet OS-keyring / Vault / customer-KMS backed, and there is no startup self-check that refuses to run if credentials are found in an unexpected location. Keyring/Vault/KMS custody and the fail-if-misconfigured self-check are **Planned** (Section 9).

---

## 6. The second local policy gate

The control plane validates intent before queuing a job (the "cloud gate" in `jobs.py`). The runner does **not** trust that validation. Before any device is touched, the runner re-runs a fail-closed policy check locally, in `netcode/runner_checks.py::local_policy_gate`, invoked at `runner_agent.py::_execute_job` lines 122–131:

```python
gate = local_policy_gate(intent, render, payload.get("policy_yaml", ""))
if not gate["ok"]:
    return {"status": "fail", ... "message": f"Blocked by local runner policy: {gate['message']}",
            "evidence": {"local_policy": gate}}
```

The gate operates on the config the runner is *about to push* — rendered from the runner's **own** templates, never the control plane's rendered output (`_execute_job` comment, lines 111–119; the runner uses `render_intent` with its own workspace). It enforces two things against the config, line by line:

1. **Blocked fragments** — any line containing a forbidden fragment (e.g. credential lines, management-plane config) is rejected.
2. **Allow-list scope** — every non-blank line must start with an allowed prefix for the change type (e.g. `add_vlan` allows `vlan `, `interface Vlan`, `   ip address `; `bgp_neighbor` allows `router bgp `, `   neighbor `, etc. — `_DEFAULT_ALLOWED`, lines 27–34). Anything outside scope is an "unexpected line" and blocks the change.

The gate is **fail-closed in three independent ways**:

- If the policy YAML cannot be parsed, it returns `ok: False` explicitly (lines 41–42: *"malformed policy must fail closed"*).
- If any blocked fragment is present, it blocks (lines 65–71).
- If any line falls outside the allowed scope, it blocks (lines 72–78).

Only a clean pass on all three returns `ok: True`. The module is deliberately dependency-light and independent of the full `StaticValidator` (docstring) *so it can be audited on its own* — a reviewer can read `runner_checks.py` end to end in a few minutes and satisfy themselves it cannot be talked into pushing forbidden config.

**Why this matters against a compromised control plane:** even if the cloud were subverted and queued a malicious job — say, a payload whose intent smuggles a credential-changing or management-plane line — the runner re-derives the config from its own templates and re-checks it locally. A forbidden line is caught at the runner, on the customer's premises, before the device connection is opened. The control plane cannot override this gate; it is executed unconditionally on the runner's side of the boundary.

---

## 7. Result signing and evidence integrity

Every result the runner uploads is signed. After executing a job, the runner computes an HMAC-SHA256 over the **canonical JSON** of the result using its enrollment-issued secret (`runner_agent.py`, lines 198):

```python
signature = hmac.new(secret.encode("utf-8"),
                     _canonical(result).encode("utf-8"),
                     hashlib.sha256).hexdigest()
```

Canonicalization (`_canonical`, line 47: `json.dumps(sort_keys=True, separators=(",",":"))`) ensures the runner and the control plane sign byte-identical input regardless of key ordering.

The control plane verifies before accepting (`runner_hub.py::submit_job_result`, lines 88–128):

- **Ownership:** the job must have been claimed by *this* runner (`job.claimed_by != runner.id` → reject).
- **State:** results are only accepted for jobs currently `running` (rejects double-submits and stale jobs).
- **Signature:** it recomputes the expected HMAC and compares with `hmac.compare_digest` (line 107) — a constant-time comparison that resists timing attacks. A mismatch is rejected *without changing job state*, so a corrupted upload leaves the job claimable for a clean retry rather than bricking it (lines 108–111).

On success, the signature is persisted alongside the job (`store.record_job_signature`), the workflow state machine advances (`state_after_lab_action`), and a workflow event is recorded with `signature_valid: True` in its evidence (lines 119–126). The result payload — including the device session transcript and per-change evidence — becomes part of the durable change record, surfaced through `/api/change/{change_id}/record` and `/api/audit/sessions`. Combined with git-native change branches (each change lives on its own branch with committed artifacts), this yields the per-change signed evidence record: intent → policy verdict → dry-run proof → applied diff → verification, signed at source.

**What signing proves today:** that a result was produced by the holder of a specific runner's secret and has not been altered in transit or at rest since upload. It upgrades the audit log from "the cloud says this happened" to "this runner attests this happened, and the attestation verifies."

**Phase 0 signing honesty — read this carefully.** Signing is **symmetric HMAC-SHA256**. The signing secret is generated by the control plane at enrollment and stored **on the control plane** (`runners.hmac_secret`, `store.py` line 143; read back in `runner_hmac_secret`). This means the control plane *can* verify signatures, and it also means the control plane technically possesses the secret needed to *forge* one. HMAC signing therefore protects evidence integrity against **transport tampering, at-rest mutation, and third parties** — it does **not** cryptographically bind evidence to the runner in a way that a *compromised control plane* could not forge. Closing that gap is exactly what the asymmetric-key upgrade in Section 9 does: runner-held private keys the cloud never sees, so evidence is provably runner-originated even if the cloud is fully owned. We call this out rather than let a reviewer assume more from the word "signed" than the Phase 0 mechanism delivers.

---

## 8. What the control plane can and cannot see

**The control plane CAN see:**

- Device **identifiers**: `id`, `host`, `platform`, `port` (from `_runner_payload`).
- The **intent YAML** and the **rendered config** for a change (queued in the payload for record-keeping, and returned by the runner as evidence).
- The **policy YAML** shipped to the runner.
- **Results and evidence**: status, messages, the EOS config-session transcript, and per-change evidence records, all uploaded by the runner.
- **Runner metadata**: name, pool, status, version, last-seen (`runners` table).
- **Change and workflow history**: the full state-machine trail.

**The control plane CANNOT see (structurally):**

- **Device credentials** — never in the payload, never uploaded, resolved only from local runner inventory (Section 5).
- **Live device sessions** — the SSH/eAPI connection is opened by the runner inside the customer network. The cloud receives the *transcript as evidence after the fact*, never an interactive session. The dry-run executes locally; apply gates on the locally-captured proof (launch-plan runner blueprint item 5).
- **Anything on the runner host** beyond what the runner chooses to upload in a result. There is no inbound channel for the cloud to read the runner's disk, environment, or inventory.

A reviewer's one-line summary: **the cloud is the system of record; the runner is the system of action. Secrets and live access live only with the system of action.**

---

## 9. Threat model

We enumerate the adversaries a mid-market reviewer cares about and state the Phase 0 mitigation and its honest limits for each.

### 9.1 Compromised control plane (named explicitly)

**This is the adversary we take most seriously**, because a self-hosted runner that blindly trusts its cloud is a documented backdoor pattern. Assume the Netcode cloud is fully owned: attacker controls the job queue and can craft arbitrary payloads.

**What they still cannot do:**

- **Obtain device credentials.** Credentials are never sent to the cloud and never uploaded; they exist only in the runner's local inventory (Section 5). There is nothing to steal.
- **Push forbidden configuration.** The runner re-renders config from its own templates and re-runs the fail-closed local policy gate before any device connection (Section 6). A malicious payload that tries to smuggle credential or management-plane lines is blocked at the runner, on-premises. The gate is fail-closed on parse errors, blocked fragments, and out-of-scope lines alike.
- **Reach devices directly.** The cloud has no inbound path to the runner and no device credentials; it cannot open a session itself.

**What a compromised control plane CAN do — stated honestly:**

- **Queue in-scope-but-unwanted changes.** If a queued change passes the local allow-list (e.g. a legitimately-shaped but operationally undesirable VLAN change), the runner will execute it. The local gate constrains *what kind* of config can be pushed, not *whether a given in-scope change is desired*. Defense in depth here is the workflow state machine, dry-run-gated apply, and per-change evidence — an out-of-band operator sees every change — but a reviewer should understand the gate is a scope guard, not an approval oracle.
- **Forge result signatures**, because in Phase 0 it holds the HMAC secret (Section 7). Evidence integrity against the cloud itself is a Phase 1 property (asymmetric keys), not a Phase 0 one.
- **Deny service** by withholding jobs or refusing results. Availability is not integrity; a hostile cloud can stop work, but cannot cause an unsafe change.

**Mitigation roadmap:** asymmetric runner-held signing keys (removes forgery capability), signed job specs verified on-runner (constrains payload provenance), and per-runner revocable mTLS identity.

### 9.2 Network attacker in the customer environment

- **No inbound surface on the runner** — outbound-only, no listener (Section 3). Nothing to port-scan or exploit inbound.
- **Egress is a one-line allowlist** — a reviewer can constrain the runner to exactly one HTTPS destination plus device reach.
- **Limit (Phase 0):** TLS enforcement and cert pinning at the agent are Planned; today the operator is responsible for the TLS posture of the `--server` URL. A man-in-the-middle on an unpinned/plain-HTTP link is an operator-configuration risk until pinning ships.

### 9.3 Stolen runner credentials

- Join tokens are **single-use and atomically consumed** (Section 4) — a stolen used token is inert.
- Runner tokens and join tokens are **stored only as SHA-256 hashes** — a database read does not yield usable bearer tokens.
- The identity file is **`0600`** on the runner host.
- **Current limit:** bearer tokens now expire, rotate, and can be revoked, but they are not mTLS certificates and the control plane still participates in the shared-secret lifecycle. Runner-held asymmetric identity remains planned.

### 9.4 Malicious or buggy runner

- Results are **ownership-checked, state-checked, and signature-verified** before they can advance a change (Section 7). A runner cannot submit results for a job it did not claim, cannot double-submit, and cannot submit an unsigned or mis-signed result.
- Jobs are **pool-scoped** — a runner only sees jobs for its own pool.

### 9.5 Cloud outage / failure domain

- The runner is a client; if the cloud is unreachable it retries polling with backoff (`runner_agent.py`, lines 184–187) and simply does no work. **Cloud down does not touch devices** — no in-flight change is forced, and git remains the source of truth. This is the deliberate inverse of the Meraki-style "cloud down → devices affected" failure domain.

---

## 10. Phase 0 vs. Planned — the honest ledger

Netcode Phase 0 is the control-plane/runner split, built and hardware-proven. The table below is the single place a reviewer should look to understand exactly where the architecture is today versus where it is going. Nothing in the "Phase 0 reality" column is aspirational; nothing in it is overstated.

| Control | Phase 0 reality (shipping) | Planned |
|---|---|---|
| **Result signing** | HMAC-SHA256, per-runner shared secret generated by and stored on the control plane (`store.py` L143). Protects integrity vs. transport/at-rest/third parties; cloud technically can forge. | Runner-held **asymmetric keys**; cloud never holds the private key. Evidence provably runner-originated even against a compromised control plane. |
| **Runner identity** | Time-bounded bearer token (hash-stored) with prepare/confirm rotation, bounded previous-token overlap, and per-runner revocation. | Locally-generated keypair → short-lived **auto-renewing mTLS certs**. |
| **Access control (admin)** | Single shared admin token via `NETCODE_ADMIN_TOKEN` (`api.py::_admin_guard`). **Open when unset** — intended for local dev only. | Full **RBAC**, per-user identity, multi-tenancy. |
| **Credential custody** | Local `inventory.yaml` on the runner host (inside customer boundary). Never in cloud payload — proven. | **OS keyring / Vault / customer KMS**; startup self-check that refuses to run if misconfigured. |
| **Transport** | HTTPS to the configured `--server`; agent will speak HTTP if pointed at one (lab default). | **Enforced TLS + certificate pinning** at the agent. |
| **Job spec provenance** | Payload is credential-free and re-validated locally; not cryptographically signed by the cloud. | **Signed job specs** verified on-runner. |
| **Store** | SQLite with WAL for local/community use; Postgres is selected through `DATABASE_URL` for the hosted pilot. | Prove backup/restore and failover behavior in the public AWS environment. |
| **Supply chain** | Standard-library-only runner (`urllib`), auditable, minimal dependencies. | **SBOM + CVE scanning, SLSA provenance, signed binaries, signed auto-update with rollback**, annual pen-test attestation. |

**On the admin token specifically:** `_admin_guard` (`api.py`, lines 536–542) returns early — i.e. **allows the request** — when `NETCODE_ADMIN_TOKEN` is unset. This is a deliberate local-development affordance and **must not** be left unset in any hosted deployment. It is called out here so no reviewer discovers it later and mistakes a dev convenience for a production posture. Minting join tokens is the one runner-facing operation it guards; in a hosted deployment the token must be set, and it is superseded by RBAC in Phase 1.

---

## 11. Summary for the reviewer

The Netcode architecture makes one claim that survives a compromised cloud: **device credentials are never in the cloud to steal, and the on-prem runner will not push out-of-scope config regardless of what the cloud tells it.** Both halves are enforced in code a reviewer can read directly — a credential-free payload builder (`jobs.py::_runner_payload`) and a self-contained, fail-closed, locally-executed policy gate (`runner_checks.py::local_policy_gate`) — and both have been demonstrated on hardware.

What Phase 0 does **not** yet provide is equally explicit: asymmetric evidence signing (HMAC today), full RBAC (shared admin token today), keyring/KMS credential custody (local file today), enforced TLS pinning, and Postgres. Each has a defined successor on the roadmap, and none is misrepresented as already present.

For a mid-market reviewer clearing this product ahead of SOC 2, the architecture's structural credential custody and local fail-closed gate are the properties that de-risk the "runner as backdoor" concern. This whitepaper, paired with the forthcoming pen-test attestation, is intended to be sufficient to clear security review; SOC 2 Type I/II follows on the roadmap timeline.

---

*All code references in this document are to the Netcode repository at version 0.1.0-phase0: `netcode/runner_agent.py`, `netcode/runner_hub.py`, `netcode/runner_checks.py`, `netcode/jobs.py`, `netcode/api.py`, `netcode/store.py`. A reviewer is encouraged to verify each cited behavior against the source directly.*
