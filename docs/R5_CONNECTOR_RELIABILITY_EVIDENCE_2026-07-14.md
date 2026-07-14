# R5 Local Connector Reliability Evidence - 2026-07-14

## Verdict

The Local Connector execution lifecycle is software-ready for a controlled Community or paid-pilot deployment. Clean-machine Windows service proof remains an R4 external gate; it is not claimed here.

## Controls now enforced

| Boundary | Enforced behavior |
| --- | --- |
| Job ownership | Every claim carries an opaque, hash-stored lease token bound to the connector, job, and expiration. |
| Lease renewal | The connector renews an active lease while work is running. Stale connectors and stale lease tokens cannot renew or submit a result. |
| Safe recovery | Expired read-only jobs may be retried within a bounded attempt limit. Expired writes are never replayed automatically. |
| Uncertain writes | An interrupted apply or rollback becomes `reconcile_required`; the change remains blocked while a read-only verification job determines actual state. Human review remains required. |
| Duplicate processes | One connector identity can hold only one active claim. Duplicate service processes cannot claim parallel work under the same identity. |
| Per-device serialization | Mutating operations for one device are serialized across connectors and request retries. Different devices may still run in parallel. |
| Idempotency | Repeated requests with the same operation identity resolve to one durable job; an idempotency key cannot be rebound to different work. |
| One-time discovery credentials | Credentials are scrubbed after claim. An interrupted discovery that no longer has credentials fails rather than replaying a redacted payload. |
| Queue bounds | Per-organization queue limits, queue age, and oldest-waiting-job alerts are durable and observable. |
| Drain and revoke | Administrator drain blocks new claims without being overwritten by heartbeats. Revocation blocks authentication and terminates connector-owned Shell sessions. |
| Credential lifecycle | Connector bearer credentials expire and rotate through a prepare/confirm protocol with bounded overlap and crash recovery. Stored tokens remain hash-only. |
| Audit | Lifecycle, security, lease, reconciliation, and execution events are persisted without exposing lease or bearer tokens. |

## Adversarial verification

The focused failure-injection suite covers process duplication, lease expiry, stale results, duplicate queue requests, write uncertainty, reconciliation, queue pressure, drain, cancellation, token rotation, token expiry, revocation, and organization isolation.

```text
tests/test_job_leases.py
tests/test_runner_operation_ledger.py
tests/test_connector_lifecycle.py
tests/test_runner_token_lifecycle.py

46 passed
```

The complete Netcode suite also passed:

```text
414 passed
```

## Relevant implementation checkpoints

- `8dbcbf8` - connector job leases and crash recovery
- `7a5866b` - operation deduplication and per-device serialization
- `8448511` - uncertain-operation reconciliation
- `1d450e3` - connector drain, cancellation, queue, and lifecycle controls
- `f642c4b` - credential expiry, rotation, and revocation

## External proof still required

- Install the signed Nuitka package on a clean Windows 11 machine with no developer tools.
- Prove supervised service start, restart, upgrade, uninstall, single-instance behavior, and proxy/custom-CA handling.
- Run a sustained public TLS/WSS connector soak against the AWS pilot environment.
- Execute the selected customer's device and controller paths before claiming those hardware combinations.

These are R2/R4/customer-hardware acceptance gates, not missing connector lifecycle software.
