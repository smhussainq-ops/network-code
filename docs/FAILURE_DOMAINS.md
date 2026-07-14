# Netcode Failure-Domain & Resilience

> **Current implementation note (2026-07-14):** This document began as the Phase 0 architecture record. Job leases, safe crash recovery, uncertain-write reconciliation, per-device serialization, connector drain, credential expiry/rotation, and revocation have since shipped. Current evidence is in `docs/R5_CONNECTOR_RELIABILITY_EVIDENCE_2026-07-14.md`; where older Phase 0 wording conflicts, the dated evidence is authoritative.

**"But it's cloud-controlled."**

Every network engineer who lived through a Meraki dashboard outage — or watched a controller push bad config to every site at once — asks this first. It is the right question. The wrong answer is to promise the cloud never goes down. The right answer is architectural: **the cloud is not in the datapath, and it cannot touch your devices.** Netcode is built so that a total control-plane failure degrades to "you can't start new changes for a while" — never to "your running network is at risk."

This document walks every failure we can name and states the *actual* behavior, cited to the code that produces it and to the live Phase 0 hardware test that proved it.

---

## The core structural fact

Netcode splits into two halves that share no trust and no reach:

- **Control plane** (`netcode/api.py`, `netcode/runner_hub.py`) — the SaaS. Holds workflow state, git branches, evidence, and the job queue. It has **no device credentials and no device reachability.** In the Phase 0 test the control plane host literally could not route to the lab subnet (`172.100.1.41 unreachable from Mac`).
- **On-prem runner** (`netcode/runner_agent.py`) — an outbound-only client next to your devices. It **dials out** and never listens on a port. It holds credentials locally, re-runs the safety checks locally, and is the *only* component that ever opens a session to a device.

Because the control plane physically cannot reach the devices, "the SaaS pushed bad config" is not a risk we mitigate — it is a state the architecture **cannot enter.** The e2e test is designed so that if the control plane could cheat, the test would still pass; it can't, so it doesn't (`network-as-code-phase0-plan.md`, "The Mac-can't-reach-lab fact is a feature").

This is the Meraki inversion: cloud-controlled becomes the *differentiator*, because the cloud's blast radius is bounded by construction, not by policy.

---

## Failure-domain table

| Failure | What is at risk | Actual behavior | Blast radius | Proven? |
|---|---|---|---|---|
| **Control plane unreachable / down** | Nothing on the device | Devices keep running untouched. In-flight session aborts safely (see below). Git artifacts on disk survive. New changes can't be *started* until it returns. | Author workflow only | Structural (no device reach) |
| **Runner offline** | A queued change | Job stays `queued`; nothing is claimed; **nothing reaches the device.** | The queued change waits | **PROVEN LIVE** — VLAN 95 never appeared |
| **Runner crashes mid-apply** | The in-flight operation | SIGTERM drains normally. An expired write lease is never replayed; it becomes `reconcile_required`, queues read-only state verification, and remains blocked for human review. | One device, one change | Lease and reconciliation failure-injection tests |
| **Network partition during a config session** | The candidate config | EOS config session is abandoned uncommitted → running-config untouched. On the runner side, `config_session` aborts on any exception. | One device, one change | Abort discipline in `lab.py` |
| **Compromised / buggy control plane pushes forbidden config** | Device integrity | Runner re-runs fail-closed policy **locally** before touching the device; forbidden config is blocked at the runner even if the cloud check is bypassed. | Blocked at the edge | Local gate in `runner_checks.py` |
| **Bad or duplicated result submitted** | Workflow integrity | HMAC-SHA256 signature verified control-plane-side; mismatch → result rejected, job left claimable, workflow **not advanced.** | Rejected, no state change | Signature check in `runner_hub.py` |
| **Duplicate request or connector process** | Duplicate device operation | Idempotency, one active claim per connector identity, and per-device mutation serialization reduce duplicate execution to one durable operation. | One durable job | Concurrency and lease tests |
| **Credential exposure via the cloud** | Device passwords | Credentials never transit the cloud; job payloads are credential-free; runner resolves creds from its own local inventory. | None (creds never leave premises) | **PROVEN LIVE** — password stripped, change still succeeded |

---

## Failure by failure

### 1. Control plane unreachable → devices unaffected, in-flight change aborts safely, git survives

The control plane is not in the datapath. It queues work and records state; it does not open sessions to devices. If it is down or unreachable:

- **Running devices are untouched.** No component with device reach depends on the control plane being up. The control plane host cannot even route to the devices (Phase 0: `172.100.1.41 unreachable from Mac`).
- **An in-flight change aborts safely.** A device change only exists inside an EOS *config session*, and that session is never committed until every command is accepted. If the runner loses the control plane mid-change, the worst case is an uncommitted session — the running-config is never partially written. (See §3 and §4 for the session mechanics.)
- **Git artifacts survive.** Change branches, rendered config, and evidence records are files on disk and commits in the repo. They do not live only in control-plane memory; a control-plane restart re-reads them.

The only thing you lose is the ability to *start* and *advance* new changes until the control plane returns. That is a workflow pause, not a network incident.

### 2. Runner offline → jobs stay queued, nothing reaches the device (PROVEN LIVE)

When `NETCODE_EXECUTION=runner`, the control plane gate-checks the change and then **enqueues** it rather than executing it (`jobs.py`, `run_lab_action` → `store.queue_job(...)`, returning `status: "queued"`). Execution happens only when a runner *claims* the job by long-polling (`runner_hub.py`, `poll_for_job` → `store.claim_next_job`). No runner, no claim, no device contact.

This is the load-bearing property, and it was **proven on real hardware** (`network-as-code-phase0-plan.md`, M4 negative proof):

> With the runner stopped, a queued dry-run stayed `queued`, the change stayed `validated`, and **VLAN 95 never appeared on the device** — the control plane structurally cannot self-execute.

The control plane has no code path that opens a device session. A queued job is inert until an on-prem process pulls it. This is the opposite of the controller-pushes-to-everyone failure mode: here, an outage of the orchestrator means *nothing happens*, which is exactly the safe default for a network.

### 3. Runner crashes mid-apply → SIGTERM drain finishes/aborts the in-flight job

The runner installs a signal handler for both `SIGTERM` and `SIGINT` that sets a stop flag rather than killing the process immediately (`runner_agent.py`):

```python
def _handle_sigterm(signum, frame):
    global _stop
    _stop = True
    print("\n[runner] SIGTERM received — will exit after the current job drains.", flush=True)
```

The poll loop checks `while not _stop:` at the **top** of each iteration, after a job completes — so a graceful shutdown (deploy, restart, `systemctl stop`) lets the current job run to a terminal state and report before the process exits. The in-flight change is never left in an ambiguous half-state on the control plane's books.

For an *ungraceful* crash (OOM, power loss) there is a second line of defense that requires no cleanup at all: the change only exists as an uncommitted EOS config session (§4). A dead runner cannot commit, so the device keeps its running-config. The blast radius of a runner crash is bounded to one device and one change, and in the common (graceful) case it is zero.

### 4. Network partition during a config session → session abort leaves running-config untouched

This is the mechanism that makes "safe by default" true at the device. All device writes go through `AristaEOSLabAdapter.config_session` (`lab.py`), which:

1. Opens a named session: `configure session netcode_<ts>`
2. Feeds each candidate line, checking every response for CLI errors
3. Shows the diff, then either **`abort`** (dry-run) or **`commit`** (apply)

Crucially, every failure path aborts:

```python
except Exception as exc:
    try:
        abort = self._send("abort")
        transcript.append({"command": "abort", "output": abort})
    except Exception:
        pass
    return LabResult(status="fail", ...)
```

A partition mid-session raises on the next send; the adapter aborts the session, and an aborted EOS session **never modifies running-config.** Even the best case — a dry-run — *always* aborts on purpose (`if action == "dry-run": final = self._send_checked("abort")`), which is why a dry-run is guaranteed side-effect-free. If the partition is so abrupt that even the `abort` can't be sent, the session simply is never committed, and EOS discards it: the device is unchanged.

The `dry_run`, `apply`, and `rollback` entry points all wrap the session in `try/finally: self.disconnect()`, so the connection is always torn down rather than left dangling.

### 5. Compromised or buggy control plane → local fail-closed gate blocks forbidden config

The control plane runs the safety gate, but Netcode does **not trust it to be the only gate.** Before the runner touches a device, it re-runs the fail-closed policy checks locally against its *own* policy file (`runner_agent.py`, `_execute_job`):

```python
gate = local_policy_gate(intent, render, payload.get("policy_yaml", ""))
if not gate["ok"]:
    return {"status": "fail", ...,
            "message": f"Blocked by local runner policy: {gate['message']}"}
```

The runner also renders from *its own* templates, not the control plane's rendered output (`runner_agent.py`: "the runner uses ITS OWN templates … so it fully controls what gets pushed"). So a control plane that is compromised, buggy, or malicious cannot smuggle forbidden config past the runner — the edge has the final veto. Phase 0's M4 plan proves this by design: kill the cloud-side check in a test build and confirm the runner check alone still blocks a `username ...` line.

### 6. Bad or duplicate result → signature verification

Every result the runner reports is HMAC-SHA256 signed with the per-runner secret issued at enrollment (`runner_agent.py`):

```python
signature = hmac.new(secret.encode(), _canonical(result).encode(), hashlib.sha256).hexdigest()
```

The control plane verifies it before advancing any state (`runner_hub.py`, `submit_job_result`), using a constant-time compare and rejecting on mismatch **without** touching job or workflow state:

```python
if not hmac.compare_digest(expected, signature or ""):
    # leave the job claimable for a retry rather than bricking it
    return {"ok": False, "message": "Result signature verification failed; result rejected."}
```

The same function also enforces ownership (`job.claimed_by != runner.id` → rejected) and single-completion (`if job.status not in ("running",)` → rejected), so a stale, replayed, or duplicate result cannot double-advance a change or corrupt the state machine. A corrupted result is rejected, the job stays claimable, and the workflow does not move.

### 7. Credentials never transit the cloud (PROVEN LIVE)

The job payload the control plane builds is deliberately credential-free (`jobs.py`, `_runner_payload`: *"Deliberately credential-free: the runner resolves credentials from its own local store by device id"*). The runner refuses to take credentials from the cloud at all — it resolves them only from `~/.netcode-runner/inventory.yaml` (`runner_agent.py`).

Proven on hardware (M4):

> Queued job payloads carried NO password; the Mac inventory had **no device password at all**, yet the change succeeded because the runner supplied creds locally.

A full compromise of the SaaS therefore yields zero device passwords. There is nothing there to steal.

---

## What we do NOT yet handle (honesty section)

Resilience claims are only credible if we're equally clear about the remaining gaps:

- **HMAC, not asymmetric signing.** Results are signed with a symmetric per-runner secret issued at enrollment. The control plane holds that secret, so it could in principle forge a runner's result. Phase 1 upgrades to runner-held asymmetric keys so the cloud can *verify* but never *forge*. (`runner_hub.py` header note.)
- **Single control-plane task for the pilot.** Organization scoping and Postgres-backed deployment are implemented, but interactive coordination still requires one control-plane task until shared coordination is proven. This limits control-plane availability, not device safety.
- **Connector HA needs environment proof.** Leases, duplicate-process protection, per-device serialization, recovery, drain, rotation, and revocation are implemented. A multi-connector customer deployment still requires a public TLS/WSS soak and failure test in that environment.
- **Apply is verify-gated but not transactional across devices.** Each change targets one device via one config session. There is no multi-device atomic commit; a change spanning several devices is several independent sessions, each individually safe but not jointly all-or-nothing.
- **Read depth varies by platform.** Discovery, verification, drift, Rez read tools, Shell, and supported APIs route through the Local Connector. The reviewed status of each platform is published by `/api/platform/capabilities`; contract-tested adapters are not hardware certifications.
- **Write support remains intentionally narrow.** Arista EOS has the live dry-run/apply/verify/rollback proof. Other direct and manager-mediated writes stay `planned` or `hardware-blocked` until proven on the customer's selected hardware.

None of these gaps put the *running network* at risk — the device-safety properties in §3–§7 hold regardless. They are workflow-integrity and operational-maturity gaps, and each has a named home on the Phase 0 → Phase 1 roadmap.

---

## The one-line version

**The cloud can go dark, get compromised, or send garbage — and your running config does not move.** New changes pause; the network doesn't. That is what "cloud-controlled" should have meant all along.

---

Source files grounding this document (all absolute paths):
- `/Users/syedhussain/Documents/Network Automation/netcode/runner_agent.py` — SIGTERM drain, poll loop, local gate, HMAC signing, credential-free execution
- `/Users/syedhussain/Documents/Network Automation/netcode/jobs.py` — credential-free queueing, cloud gate, execution-mode switch
- `/Users/syedhussain/Documents/Network Automation/netcode/lab.py` — EOS config-session abort discipline
- `/Users/syedhussain/Documents/Network Automation/netcode/runner_hub.py` — signature verification, ownership/single-completion checks, workflow advance
- `/Users/syedhussain/Documents/Network Automation/network-as-code-phase0-plan.md` — live-proven M4 results (VLAN 94/95, credential-custody, runner-offline negative proof)
