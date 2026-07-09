# Netcode Shell + Governed Automation Validation - 2026-07-09

## Scope

Validate that the temporary ORB-only shell fix is now durable in Netcode source, and prove the pilot-critical user stories against the live ORB Arista lab:

- Netcode Shell supports line-by-line live SSH configuration through the local runner.
- The control plane does not hold device credentials or connect directly to devices.
- Governed automation still follows plan -> dry-run -> second-person approval -> apply -> verify -> rollback.

## Source Commit

- `44e332b netcode: persist REST shell sessions on runner`

Files changed:

- `netcode/api.py`
  - `/api/shell/input` now sends `session_id` to the runner.
- `netcode/runner_agent.py`
  - REST shell calls reuse a persistent `AristaEOSLabAdapter` per shell session.
  - Cache key is `session_id` when available, falling back to `device:<id>`.
  - Idle adapters are evicted after 300 seconds.
  - Adapter is dropped on execution error.
- `tests/test_platform_core.py`
  - Regression proves one session preserves config mode across separate REST calls.
  - Regression proves a second session to the same device does not inherit config mode.

## Automated Tests

Command:

```bash
cd "/Users/syedhussain/Documents/Network Automation"
./.venv/bin/python -m py_compile netcode/runner_agent.py netcode/api.py
./.venv/bin/python -m pytest tests/test_platform_core.py::test_runner_rest_shell_persists_cli_mode_per_session -q
./.venv/bin/python -m pytest tests/ -q
```

Result:

- Targeted shell regression: `1 passed`
- Full Netcode suite: `141 passed`

## Deployment

Deployed committed source to ORB runner:

```bash
tar --exclude='__pycache__' --exclude='*.pyc' -czf - netcode templates | \
  orb -m clab bash -lc 'cd /home/syedhussain/netcode-runner-app && tar -xzf - && python3 -m py_compile netcode/runner_agent.py netcode/lab.py netcode/models.py netcode/change_types.py netcode/shell_guard.py netcode/shell_pty.py'
```

Restarted:

- Netcode control plane on `http://127.0.0.1:8095`
- One ORB runner process only

Confirmed:

- `pgrep -af "python3 -m netcode.runner_agent run"` showed exactly one runner process.
- `/api/runners` showed one online runner in pool `store-lab`.

## Live Proof 1 - Netcode Shell Line-by-Line Config

Device:

- `v2-store1`

User-path tested:

- Open shell through `/api/shell/open`
- Send each command as a separate `/api/shell/input` request, matching the browser UI behavior.

Commands:

```text
configure terminal
interface Loopback101
description NETCODE_LINE_BY_LINE_PROOF
end
show running-config interfaces Loopback101
```

Observed output:

```text
configure terminal
v2-store1(config)#

interface Loopback101
v2-store1(config-if-Lo101)#

description NETCODE_LINE_BY_LINE_PROOF
v2-store1(config-if-Lo101)#

show running-config interfaces Loopback101
interface Loopback101
   description NETCODE_LINE_BY_LINE_PROOF
v2-store1#
```

Cleanup:

```text
configure terminal
no interface Loopback101
end
show running-config interfaces Loopback101
```

Post-cleanup output contained no `NETCODE_LINE_BY_LINE_PROOF`.

Result:

- `line_by_line_live_shell=PASS`

Important finding:

- The first live attempt failed because two runner processes were polling the same queue. `conf t` and `interface` were processed by different processes, splitting shell state. After clearing duplicates and running exactly one foreground runner, the proof passed. The operational requirement is one active runner process per runner identity/pool unless the server adds session affinity.

## Live Proof 2 - Governed Automation E2E

Change:

- Add VLAN `3996` named `NETCODE_E2E_PROOF` on `v2-store1`

Flow:

1. `POST /api/desired-state/plan`
2. `POST /api/lab/dry-run`
3. Wait for runner job completion
4. `POST /api/change/{id}/approve` with `approved_by=alex`
5. `POST /api/lab/apply`
6. Wait for runner job completion
7. `POST /api/verify/intent`
8. `POST /api/lab/rollback`
9. Wait for runner job completion
10. `POST /api/verify/intent` again to prove the VLAN is absent after rollback

Result:

```text
START state= dry_run_passed
APPROVE ok= True state= approved
APPLY job status= completed result_status= pass
AFTER_APPLY state= rollback_available apply_present= True
VERIFY ok= True status= pass
ROLLBACK job status= completed result_status= pass
AFTER_ROLLBACK state= rolled_back rollback_present= True
POST_ROLLBACK_VERIFY ok= False status= fail message= Could not prove VLAN 3996 with name NETCODE_E2E_PROOF exists.
RESULT governed_automation_e2e=PASS
```

Change id:

- `4d0c578b-6cd1-40c7-94a1-7ebdad4ef386`

Interpretation:

- Dry-run used EOS config session proof and aborted before write.
- Apply required a second approver.
- Apply succeeded through runner.
- Verification proved the VLAN existed after apply.
- Rollback succeeded through runner.
- Post-rollback verification failed to find the VLAN, proving cleanup.

## Website Status

The website copy has been updated locally to describe Netcode Shell as full live SSH:

- `/Users/syedhussain/Dev/Prod/resonance-core/website/v1/index.html`

Current relevant phrases:

- `Full live Netcode Shell`
- `Open full live SSH sessions through the local runner...`
- `Mode full live SSH`

Not committed in this pass:

- `website/v1/index.html` contains a large pre-existing redesign diff (`+700/-454`) unrelated to this Netcode source fix. Committing it here would bundle unrelated website work.

## Open Follow-Ups

1. Add server-side session affinity or duplicate-runner protection so one shell session cannot be split across multiple runner processes in production.
2. Decide whether to commit the existing website redesign as its own website checkpoint.
3. Optional: expose the live proof status in the UI so Marcus can see "line-by-line shell mode active" and "runner connected" before typing config.
