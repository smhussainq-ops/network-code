# AWS Pilot Deployment Plan — Netcode + Rez (for Codex)

**Date:** 2026-07-06
**Goal:** Deploy the Rezonance backend (Netcode control plane + Rez chat-v2) to AWS so **1–2 pilot customers** can run it against their own devices via a customer-installed runner. Grounded in verified code state of both repos.
**Scope discipline:** pilot-grade, not GA. Single-task per service (no horizontal scale), Bedrock LLM, one runner per customer. Hardening (Redis shared state, autoscaling, HA) is explicitly deferred (§8).

Repos:
- Netcode control plane: `~/Documents/Network Automation` (FastAPI `netcode.api:app`).
- Rez chat-v2 server: `~/Dev/Claude/resonance-core` (FastAPI `server:app`).

---

## 1. Target architecture on AWS

```
CUSTOMER NETWORK                          AWS (one region, e.g. us-east-1)
┌─────────────────────────┐               ┌───────────────────────────────────────────┐
│  Runner (Win/Linux)     │  outbound     │  ALB (TLS 443, WebSocket enabled)          │
│  netcode-runner         │  HTTPS/WSS    │     │                     │                 │
│   • local inventory     │──────────────▶│     ▼                     ▼                 │
│   • device credentials  │   (dials out) │  Netcode CP            Rez chat-v2          │
│   • SSH/API to devices  │               │  ECS Fargate (1 task)  ECS Fargate (1 task) │
└───────────┬─────────────┘               │   uvicorn :8095         uvicorn :8080       │
            │ local SSH/API                │   NETCODE_EXECUTION=    REZ_RUNNER_SPLIT=   │
            ▼                              │     runner                true              │
     Customer devices                     │      │  │                  │                │
                                          │      │  └── EFS workspace   ├─ Bedrock (IAM) │
                                          │      ├── RDS Postgres       └─ EFS incidents │
                                          │      └── Secrets Manager                     │
                                          │  Rez ──(internal, NETCODE_REZ_BRIDGE_TOKEN)──▶ Netcode /api/rez/runner-read
                                          └───────────────────────────────────────────┘
                                          Browser → ALB → Netcode UI + Rez chat-v2 SSE
```

**Trust boundary:** device credentials + SSH/API sockets live ONLY on the customer's runner. The AWS backend never holds a credential or dials a device — it queues jobs the runner pulls (outbound long-poll + persistent WSS). This is already enforced in code (Slices 1–6 validated).

---

## 2. What is ALREADY ready (do NOT rebuild)

- **Netcode Postgres**: `store.py` has a full engine abstraction gated on `DATABASE_URL` (SQLite default; `postgres://|postgresql://` → psycopg, `?`→`%s` rewrite, idempotent `CREATE TABLE IF NOT EXISTS` schema incl. Postgres `information_schema` column checks). Moving off SQLite = set `DATABASE_URL` + install the `postgres` extra. No schema code change.
- **Netcode runner data plane**: `/api/runner/enroll|poll|heartbeat|jobs/{id}/result` (HTTP long-poll, runner-token auth, per-runner HMAC-signed results) + persistent WS `/api/runner/stream`. Cloud-ready; auth bypasses user RBAC correctly (`/api/runner/*`).
- **Enrollment flow**: join-token (`njt_`, single-use, atomic consume) → mints runner token (`nrt_`) + HMAC secret. A customer can self-enroll a runner with a token.
- **Runner is HTTPS/WSS-ready**: `runner_agent.py:1269` derives `wss://` from `https://`; HTTP calls use the server URL as-is over TLS. **No runner protocol code change** for cloud — only config (`identity.json` server URL) + packaging.
- **Rez server is container-ready with Bedrock**: ships a production multi-stage `Dockerfile` (`python:3.12-slim`) that extracts the SDK-bundled `claude` CLI binary to `/app/rez-engine` and defaults to `CLAUDE_CODE_USE_BEDROCK=1`. **CLI-in-Linux-container is the shipped design** (binary bundled in the `claude-agent-sdk` pip package — no Node/npm at runtime). Bedrock via IAM role = no API-key secret.
- **The bridge seam**: Rez `rez_runner_client.py` → `POST {CP}/api/rez/runner-read` (Bearer `NETCODE_REZ_BRIDGE_TOKEN`), served by Netcode `api.py:844` (action allowlist + credential-field stripping). Proven end-to-end for all 6 tool families.

---

## 3. Pre-pilot fixes (do these FIRST — small, real)

1. **Netcode `ws_runner_stream` null-runner bug** (`api.py:~1345`): `authenticate_runner` can return `None`, then `.pool` raises `AttributeError` (currently swallowed by a broad except → silent close). Handle `None` explicitly (close with a clear reason).
2. **Default admin creds**: Rez packaged builds default to `admin/admin123` (`server.py:71-72`); Netcode has `NETCODE_BOOTSTRAP_ADMIN_*`. Both MUST be real secrets before any public exposure.
3. **Bridge token must be set**: `NETCODE_REZ_BRIDGE_TOKEN` unset = open bridge. Require it in cloud (fail fast if empty when `NETCODE_EXECUTION=runner`).
4. **Single-worker guardrail**: Netcode holds `_RUNNER_CHANNELS` / `_SHELL_SESSIONS` / `_BROWSER_SOCKETS` and background schedulers in process memory. Deploy each service as **exactly one ECS task, uvicorn single worker**. Add a startup log/assert that warns if `WEB_CONCURRENCY>1`.
5. **Slice-6 dead-code gate** (Rez `sdk_tools/__init__.py:49`): `create_rez_mcp_server` registers `rez.validate` + broad math/ops with no split check. Unreachable today but harden with the `_should_register_validate_tools` gate to prevent a future regression.

---

## 4. Build phases (sequenced to de-risk)

### Phase A — Containerize Netcode CP
- Add a `Dockerfile` for Netcode (`python:3.12-slim`, `pip install .[postgres]`, `git` present for gitflow, `CMD uvicorn netcode.api:app --host 0.0.0.0 --port 8095 --workers 1`).
- `HEALTHCHECK` → `/api/health`.
- Rez already has its Dockerfile — reuse it.

### Phase B — AWS infra (IaC: Terraform or CDK)
- **VPC** with public (ALB) + private (Fargate, RDS) subnets.
- **RDS Postgres** (single-AZ pilot, encryption-at-rest via KMS — required: per-runner `hmac_secret` is stored plaintext in the `runners` table).
- **EFS** — two access points: Netcode workspace (`intents/`, `rendered/`, `reports/`, git working tree) and Rez runtime (`incidents/`, `state/`, sqlite memory DBs). Both are ephemeral-in-container otherwise → session/memory loss on restart.
- **ECS Fargate**: 2 services, 1 task each — `netcode-cp` (:8095) and `rez-server` (:8080).
- **ALB** (TLS via ACM cert + a real domain): HTTP→HTTPS redirect; path/host routing to the two services; **WebSocket support + idle timeout ≥ 60s** (covers the 20–25s runner long-poll and the persistent `/api/runner/stream` + `/api/shell/session/*` channels).
- **Bedrock**: enable model access (Claude Sonnet 4.5 + Haiku inference profiles) in-region; Fargate task role grants `bedrock:InvokeModel` + `InvokeModelWithResponseStream`.
- **Secrets Manager / SSM**: all tokens + DB creds (§5).

### Phase C — Config & secrets wiring (§5 matrices).

### Phase D — Runner productionization (Linux first)
- **No protocol code change needed** (HTTPS/WSS confirmed). Work items:
  - **Enrollment UX**: a `netcode-runner enroll --server https://... --token njt_...` flow that writes `identity.json` (the `enroll` subcommand exists — confirm it persists identity + accepts the cloud URL).
  - **Inventory onboarding**: the runner needs the customer's FULL device list in its local `inventory.yaml` (see §7 — the `v2-hq-edge-2` test fell back to snapshot because it wasn't in inventory). Provide an import path (NetBox sync, CSV, or discovery).
  - **Service wrapper**: systemd unit (Linux) so it survives reboot and restarts on failure.
  - **Packaging**: a versioned tarball/pip-installable + a one-command install script.

### Phase E — Windows runner (separate, after Linux proves out)
- Runner is Windows-viable (stdlib + paramiko + threading; no `pty`/`termios`/`fork`; only cosmetic SIGTERM). Prove on native Windows: wheels install (paramiko/netmiko/websocket-client), runs as a **Windows Service** (NSSM or `pywin32`), `identity.json` under `%USERPROFILE%\.netcode-runner`.
- Package a Windows installer (`.msi`/`.exe`) that installs the service + runs enrollment.

### Phase F — End-to-end validation (§6).

---

## 5. Env / secrets matrices

### Netcode CP (ECS task)
| Var | Value | Secret? |
|---|---|---|
| `DATABASE_URL` | `postgresql://…@rds…:5432/netcode` | ✅ |
| `NETCODE_EXECUTION` | `runner` | |
| `NETCODE_RUNNER_POOL` | e.g. `cust-acme` (per customer) | |
| `NETCODE_AUTH` | `1` | |
| `NETCODE_ADMIN_TOKEN` | break-glass bearer | ✅ |
| `NETCODE_BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` | seed admin | ✅ (pwd) |
| `NETCODE_REZ_BRIDGE_TOKEN` | shared with Rez | ✅ |
| `NETCODE_WORKSPACE` | EFS mount path | |
| `NETCODE_REQUIRE_APPROVAL` | `1` (recommended for pilot) | |
| `NETBOX_URL` / `NETBOX_TOKEN` | only if NetBox SoT | ✅ (token) |

### Rez chat-v2 (ECS task)
| Var | Value | Secret? |
|---|---|---|
| `REZ_USE_SDK` | `true` | |
| `CLAUDE_CODE_USE_BEDROCK` | `1` | |
| `AWS_REGION` | `us-east-1` | |
| `REZ_CLAUDE_CLI_PATH` | `/app/rez-engine` | |
| `REZ_RUNNER_SPLIT_MODE` | `true` | |
| `REZ_RUNNER_CONTROL_PLANE_URL` | internal Netcode CP URL | |
| `REZ_RUNNER_BRIDGE_TOKEN` | == `NETCODE_REZ_BRIDGE_TOKEN` | ✅ |
| `REZ_V2_MATH_TOOLS` | `true` (enables `rez.validate`) | |
| `REZ_INCIDENT_DIR` / `REZ_RUNTIME_DIR` / `REZ_MEMORY_DB_PATH` | EFS paths | |
| `REZ_BOOTSTRAP_ADMIN_USERNAME` / `_PASSWORD` | override admin/admin123 | ✅ (pwd) |
| `REZ_PRIVACY_ENABLED` / `REZ_PRIVACY_MASTER_KEY` | if anonymization on | ✅ (key) |
| (Bedrock auth) | IAM task role — no key env | — |

### Runner (`identity.json`, per customer)
`server=https://<pilot-domain>`, `pool=cust-acme`, `runner_token`/`hmac_secret` from enrollment. `inventory.yaml` = full customer device list + credentials (stays local).

---

## 6. Acceptance tests (must pass before customer access)

1. **Backend health**: both ECS tasks healthy behind ALB TLS; browser reaches Netcode UI + Rez chat-v2 over HTTPS.
2. **Runner over the internet**: enroll a runner (start with the existing ORB Linux runner pointed at the cloud URL), confirm `poll`/`heartbeat`/`/api/runner/stream` (WSS) connect through the ALB; `/api/runners` shows it online.
3. **Device isolation (the capstone)**: with the Netcode + Rez tasks having **no network path to the customer/lab device subnets** (security-group/NACL deny), run a chat-v2 investigation → it completes entirely through the runner. If the backend could reach devices, this would be a soft pass — enforce the deny.
4. **Credential boundary**: no device secret in CP logs, job payloads (scrubbed on claim), SSE, or Bedrock prompts.
5. **Bridge auth**: bad `NETCODE_REZ_BRIDGE_TOKEN` → 401, no fallback (already proven locally).
6. **Persistence**: restart both tasks → sessions/incidents/memory survive (EFS), changes/runners survive (RDS).
7. **Full change flow**: plan → approval gate → dry-run → apply → verify on a customer/lab device through the runner, with signed evidence.

---

## 7. Pilot onboarding flow (per customer)

1. Create a customer **org** + **pool** (`cust-acme`) + a bootstrap operator login.
2. Generate a single-use **join token**; give the customer the runner installer + `enroll` command.
3. Customer installs the runner (service), enrolls, and imports **their full device inventory** into the runner's local `inventory.yaml`.
   - ⚠️ **Inventory completeness is load-bearing.** In testing, a chat-v2 investigation of `v2-hq-edge-2` fell back to *snapshot* data (reported 51 min uptime; live was 58 min) solely because that device wasn't in the runner inventory — the split correctly refused to dial it from the control plane. Every device the customer wants live must be in the runner inventory, or Rez silently degrades to snapshot. Make inventory import a first-class onboarding step (NetBox sync or discovery sweep).
4. Smoke test: a read-only Rez investigation + one gated VLAN/NTP change on a non-critical device.

---

## 8. Explicitly DEFERRED (not for the 1–2 customer pilot)

- Horizontal scale / multi-worker: requires moving `_RUNNER_CHANNELS`/`_SHELL_SESSIONS`/`_BROWSER_SOCKETS` + schedulers to Redis. Single task is fine at pilot volume.
- RDS Proxy / pgbouncer (store opens a connection per op) and `SELECT … FOR UPDATE SKIP LOCKED` in `claim_next_job` — optimizations, not correctness, at pilot scale.
- Multi-region / HA / autoscaling.
- Full multi-tenant hardening beyond org scoping (already present).
- Runner auto-update.

---

## 9. Recommended order of execution
Pre-pilot fixes (§3) → Phase A (Netcode Dockerfile) → Phase B (infra) → Phase C (config) → Phase D (Linux runner, enroll existing ORB runner at the cloud URL) → §6 tests 1–7 → Phase E (Windows runner) → §7 onboard customer 1. **De-risk rule: prove the cloud backend with the Linux runner before introducing the Windows variable.**
