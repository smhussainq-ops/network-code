# Netcode Shell Device Catalog POC Certification

Date: 2026-07-09 (America/New_York)

## Verdict

**GO for the Mac/ORB POC Shell path.** The prior `Unknown device v2-hq-core` failure is fixed without a device-specific alias or hardcoded inventory entry.

This certifies the Shell device-catalog and Local Connector path. It does not certify the entire platform for production or AWS multi-region deployment.

## Root Cause

The Shell UI discovered devices by running a full-fleet readiness job against the Local Connector inventory. Shell open then validated the selected device against a different control-plane YAML inventory. `v2-hq-core` existed locally and was reachable, but it was absent from that YAML, so Shell returned `Unknown device` before opening SSH.

## Certified Architecture

```text
Discovery or local inventory
        |
        v
Local Connector publishes public metadata only
        |
        v
Canonical tenant device catalog
  - canonical ID
  - aliases: hostname, IP/FQDN, host:port
  - site, role, platform
  - exact connector assignment
        |
        v
Rez UI server-side search (maximum 50 rows)
        |
        v
Shell session bound to assigned connector
        |
        v
Connector resolves local credentials and opens SSH PTY
```

Device usernames, passwords, API tokens, private keys, and credential profiles remain on the Local Connector. The SaaS receives searchable public metadata and terminal frames, not device credentials.

## Delivered

- Durable, tenant-scoped `device_catalog` and indexed `device_aliases` tables.
- Local Connector inventory synchronization at startup and after inventory changes.
- Discovery results immediately register against the connector that collected them.
- Case-insensitive resolution by canonical ID, hostname, IP, FQDN, `host:port`, and explicit aliases.
- Exact connector routing for Shell sessions, including multiple connectors in one pool.
- Server-side paginated search with a hard 50-record response limit.
- Shell UI with search, site/role/vendor filters, Recent, Favorites, and Add by IP/FQDN.
- Add by IP/FQDN performs read-only identification through the Local Connector and uses connector-local credential defaults.
- Duplicate canonical IDs cannot be reassigned by another connector silently.
- Catalog synchronization rejects credential fields and non-scalar/nested payload tricks.
- Full-fleet readiness/SSH scans were removed from Shell page load.

## Acceptance Results

| Acceptance | Result |
|---|---|
| Initial Shell render under 2 seconds | **PASS: 380 ms** from Ops Dashboard click to visible `v2-hq-core` row |
| Zero device connections on page load | **PASS:** catalog response reports `device_connections_opened: 0`; no read jobs created |
| Search returns at most 50 records | **PASS:** API enforces `limit <= 50`; UI clamps requests to 50 |
| 10,000-device catalog | **PASS:** 10,000-row sync 0.1441 s; exact search 0.022718 s on the Mac test environment |
| Every displayed connectable device maps to an online connector | **PASS:** `connectable` derives from the live connector WebSocket, not stale heartbeat state |
| Live `v2-hq-core` lookup | **PASS:** canonical ID resolves to `172.100.1.11`, connector `637ced0e-...` |
| Live Shell open | **PASS:** PTY opened through the assigned ORB Local Connector |
| Live command output | **PASS:** `show version \| include Uptime` returned `Uptime: 23 hours and 20 minutes` |
| Add by IP/FQDN | **PASS:** existing `172.100.1.11` was read-only discovered and cataloged without cloud credentials |
| Credential isolation | **PASS:** snapshot and API adversarial tests contain no credential fields |

## Automated Validation

```text
Netcode full suite: 154 passed
Device catalog contracts: 7 passed
Rez Shell/catalog UI contracts: 8 passed
Python syntax checks: passed
Changed-file ESLint: passed
git diff --check (both repos): passed
```

The Rez UI repository's full TypeScript production build remains red from a broad pre-existing error set outside these Shell files. The changed Shell/catalog files pass ESLint and targeted Vitest, and the Vite POC UI is live-proven. Cleaning the repository-wide TypeScript baseline remains a production deployment task.

## Remaining Production Hardening

- Replace in-process Shell session and WebSocket maps with a shared session broker before running multiple control-plane replicas.
- Add incremental/chunked inventory synchronization for catalogs materially larger than the 10,000-device POC gate.
- Add connector selection or site-to-connector policy for discovery when a tenant has multiple non-HA connectors in the same pool.
- Add catalog stale-device retention policy and an operator conflict-resolution screen.
- Run the same certification from the packaged Windows Local Connector against a Windows-hosted GNS3 environment.
- Repair the repository-wide TypeScript production-build baseline before AWS deployment.
