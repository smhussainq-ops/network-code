# Rezonance Community Windows Launch Plan

**Date:** 2026-07-12

**Status:** Implementation plan for review

**Primary outcome:** Three real Community users install Rezonance on Windows, discover up to 25 devices, use Digital Twin and Shell locally, and complete at least one read-only Rez investigation without Rezonance paying their model-inference bill.

## Product decision

Community is a local-first Windows product, not one AWS deployment per user.

The Community installation runs these components on the user's machine:

- Rezonance desktop shell and web UI;
- Netcode local control plane;
- Rez Chat V2 and deterministic RCA services;
- Local Connector for discovery, SSH, API, Shell, and approved Netcode jobs;
- SQLite Network Model, observations, incidents, and audit records;
- a local Git change-history repository;
- a provider adapter that uses a user-owned API credential or an optional local model.

AWS is limited to the public website, signed-download/update metadata, optional license activation, and opt-in aggregate telemetry. It does not host a dedicated Community control plane and does not receive device or model-provider credentials.

## Important provider correction

Do not promise that an OpenAI or Anthropic consumer subscription can always be used through a Rezonance sign-in redirect.

- OpenAI documents ChatGPT and API billing as separate products. ChatGPT Plus/Pro does not fund API usage: https://help.openai.com/en/articles/8156019-how-can-i-move-my-chatgpt-subscription-to-the-api
- OpenAI advises against putting API keys in browser/mobile client code: https://help.openai.com/en/articles/5112595-best-practices-for-api-key
- Anthropic's product guidance for software built for others prefers Claude Console API keys or a supported cloud provider, even though some Agent SDK/subscription behavior is currently available and subject to change: https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account
- Anthropic's API supports API keys and Workload Identity Federation: https://platform.claude.com/docs/en/manage-claude/authentication

Therefore the launch contract is:

1. **OpenAI API:** user supplies their own API-platform key and billing account.
2. **Anthropic API:** user supplies their own Claude Console API key and billing account.
3. **Local model:** optional later provider for users who want no external inference, after tool-calling quality passes the same RCA gates.
4. **Provider OAuth/subscription login:** experimental only after the provider explicitly supports third-party product use and the integration passes legal, security, billing, and revocation review. It is not a Community launch dependency.

The setup UI may link users to the official OpenAI Platform or Claude Console to create a key. It must not imply that signing into ChatGPT or Claude automatically grants API usage.

## Current code baseline

| Capability | Current state | Launch gap |
|---|---|---|
| Windows package endpoint | Implemented ZIP with runner source, Rez device drivers, PowerShell install, and no embedded secrets | Requires Python and manual PowerShell; not a signed EXE/MSIX |
| Outbound Local Connector | Implemented and live-proven on Linux/ORB | Native Windows/GNS3 journey is not certified |
| Local storage | SQLite and local Git are implemented | Need one local supervisor, backup/export, and clean uninstall |
| Digital Twin | Implemented with physical, L2/LLDP, BGP, OSPF, site, path, and approved-model overlays | Enforce a deterministic 25-device Community boundary and local onboarding |
| Shell | Interactive WebSocket/PTTY path and durable transcripts are implemented | Package the desktop client and prove Windows terminal behavior |
| Rez provider abstraction | OpenAI, Anthropic, and Ollama abstractions exist for several LLM paths | Chat V2's principal agent loop still depends on Claude Agent SDK semantics |
| Anthropic direct mode | Implemented | Secure credential storage and Community setup UX required |
| OpenAI direct mode | Implemented for generic chat/tool calls | Full Chat V2/MCP investigation parity is not yet implemented |
| AI configuration | Bedrock/direct Anthropic settings exist | Secrets are persisted in `state/ai_provider_config.json`; replace before release |
| Licensing | Signed license tokens and query metering exist | No explicit Community entitlement or 25-device model cap |
| Network Model | Implemented and validated | Add Community bootstrap and backup/export UX |

## Community entitlement

### Included

- up to 25 active canonical network devices;
- unlimited local Digital Twin viewing within that device boundary;
- unlimited local Shell sessions and transcript history within that boundary;
- discovery, inventory, site assignment, and Network Model review;
- deterministic validations and math engines;
- Rez read-only investigations using the user's configured provider;
- local Git-backed change history;
- sample workflow packs and plan/dry-run preview;
- exportable diagnostics bundle with explicit redaction.

### Not included by default

- Rezonance-funded model inference;
- shared SaaS workspaces, SSO, enterprise RBAC, or cloud retention;
- distributed Local Connector fleets;
- production-scale Netcode writes unless separately licensed/enabled;
- monitoring integrations that require a public webhook endpoint;
- enterprise support or HA.

### Device-count contract

Count active canonical managed devices, not aliases, stale observations, discovered external next hops, or cloud/service pseudo-nodes.

- The 25th device succeeds normally.
- The 26th device becomes a reviewable discovery proposal but is not imported, collected, or rendered as managed.
- Existing 25 devices remain usable when an over-limit proposal appears.
- Retiring one device frees one slot without deleting its history.
- The check is enforced in the local control-plane API and Local Connector import path, not only in the UI.

## Target Windows architecture

```text
Rezonance Community Desktop
  |-- UI on loopback only
  |-- Netcode local API
  |-- Rez local API / Chat V2
  |-- Local Connector worker
  |-- SQLite + local Git
  |-- Windows Credential Manager / DPAPI
  |     |-- device credentials
  |     `-- user-owned model API credential
  |
  |-- SSH/API --> local network devices
  `-- HTTPS --> selected model provider

Optional outbound services:
  - signed update manifest
  - license activation
  - opt-in redacted product telemetry
```

All local HTTP/WebSocket listeners bind to `127.0.0.1`. Each service uses a generated local session token. No inbound LAN listener is enabled by default.

## Implementation slices

### Slice 1: Community product boundary

- Add a signed `community` entitlement.
- Enforce 25 active canonical devices in catalog import, discovery acceptance, and model activation.
- Keep Digital Twin and Shell unlimited within the device cap.
- Make deterministic/read-only features work when no LLM is configured.
- Remove the current license-lock experience for valid Community installs.

**Adversarial gate:** aliases, retire/re-add, concurrent imports, and repeated scans cannot bypass the cap or corrupt existing devices.

### Slice 2: Local all-in-one runtime

- Add a supervisor that starts Netcode, Rez, UI, and Local Connector with generated loopback credentials.
- Use SQLite and isolated local Git by default.
- Add health, restart, backup, restore, and diagnostic-bundle commands.
- Ensure Shell/Digital Twin still work when the model provider is offline.

**Adversarial gate:** no service binds to `0.0.0.0`; a compromised browser tab cannot call privileged local APIs without the local session token.

### Slice 3: Windows credential custody

- Replace plaintext provider persistence with Windows Credential Manager backed by DPAPI.
- Move device passwords, SSH keys, API tokens, and SNMP secrets out of inventory YAML.
- Keep only credential references in the local inventory.
- Add import/migration from the current YAML format and securely rewrite/redact the source.
- Redact secrets from logs, transcripts, support bundles, crash reports, and model prompts.

**Adversarial gate:** filesystem search, logs, process arguments, crash dumps, and exported bundles contain no credential value.

### Slice 4: Signed Windows installer

- Replace the current Python-dependent ZIP as the default experience.
- Package a versioned `Rezonance-Community-Setup.exe` using a bundled Python runtime and onedir application layout.
- Install per-user by default; install a Windows service only when explicitly requested.
- Include uninstall, repair, log collection, and signed update verification.
- Code-sign the installer and binaries; publish SHA-256 checksums.
- Keep the existing ZIP only as an advanced/developer artifact.

**Adversarial gate:** install on a clean Windows 11 VM with no Python, run after reboot, upgrade in place, and uninstall without leaving secrets.

### Slice 5: Provider-neutral investigation contract

- Define one `InvestigationRuntime` interface for streaming, tool calls, resume, cancellation, usage, and structured final output.
- Preserve the same read-only MCP/tool allowlist and deterministic RCA gate for every provider.
- Keep model narration separate from canonical math/root selection.
- Store provider, model, token usage, and data-routing disclosure with each incident.

**Adversarial gate:** changing providers cannot change tool permissions, authorize a write, bypass fresh-data requirements, or promote an unconfirmed RCA.

### Slice 6: Anthropic and OpenAI Community adapters

- Anthropic adapter: direct Claude API/Agent SDK using a user-owned Console key.
- OpenAI adapter: implement the full Chat V2 agent loop on the current OpenAI tool-calling/Responses surface, not only generic chat completion.
- Add provider health check, model selection, spend warning, timeout, cancellation, and rate-limit behavior.
- Add a `No provider` mode for Digital Twin, Shell, and deterministic checks.
- Do not silently fall back from one billable provider to another.

**Adversarial gate:** run the same frozen RCA set against both providers; canonical root, confidence gate, evidence scope, and Netcode draft eligibility must agree even if prose differs.

### Slice 7: First-run Marcus experience

The first-run wizard contains five screens:

1. Choose local-only Community.
2. Add device credentials to the Windows vault.
3. Discover or import up to 25 devices.
4. Review sites and approve the initial Network Model.
5. Configure Anthropic/OpenAI API access or continue without Rez narration.

The home screen then offers three clear jobs:

- Map my network with Digital Twin.
- Open a live Shell session.
- Investigate a connectivity problem with Rez.

**Adversarial gate:** no marketing/demo data appears as live customer data, and every unavailable action explains the exact missing prerequisite.

### Slice 8: Privacy, safety, and supportability

- Default to local processing and explicit provider disclosure.
- Make anonymization available and clearly show its active state.
- Keep Rez device operations read-only; Netcode writes remain a separate human-approved capability.
- Add signed support bundles with preview and user consent.
- Add backup encryption, retention controls, and factory reset.
- Publish a concise data-flow/security page in the app.

**Adversarial gate:** offline, provider failure, malformed device output, hostile prompt content, and interrupted upgrades fail closed without losing the Network Model.

### Slice 9: Native Windows/GNS3 certification

Use a clean Windows 11 VM and a Windows-reachable GNS3 lab.

1. Install from the signed EXE with no Python preinstalled.
2. Add credentials through the vault UI.
3. Discover at least five multi-vendor devices.
4. Approve site grouping and render Digital Twin.
5. Open Shell, resize, reconnect, and verify transcript history.
6. Ask one live scoped reachability question.
7. Confirm Rez uses the selected provider and Local Connector reads only.
8. Create a typed Netcode draft from a confirmed root without applying it.
9. Restart Windows and prove state, credentials, history, and services survive.
10. Attempt device 26 and prove the fail-closed entitlement behavior.

**Gate:** Marcus completes the journey without PowerShell, Python, YAML, or manual service management.

### Slice 10: Three-user Community launch

Release a private beta only after Slice 9 passes.

- Publish a `/community` page with Windows requirements, privacy model, screenshots, checksum, and release notes.
- Use a short application form: role, lab/platforms, device count, and one problem they want to solve.
- Select three users with different environments: GNS3/EVE-NG, a small production/multi-site network, and an MSP/network consultant lab.
- Offer a 30-minute private onboarding session and a written five-step challenge.
- Use a brand account and Rezonance email; do not misrepresent employee/customer identity or fabricate grassroots adoption.
- Collect only opt-in product events and scheduled feedback.

**Activation definition:** installed, 3+ devices discovered, one Digital Twin opened, one Shell session completed, and one Rez investigation attempted.

**Success definition:** all three activate, at least two complete a useful live investigation, at least two return within seven days, and no credential/privacy incident occurs.

## Fastest route to three users

### Week 1: product gate

1. Complete Slices 1-4 and Anthropic direct-key Community mode.
2. Certify on one clean Windows/GNS3 machine.
3. Record a two-minute installation-to-live-map walkthrough.
4. Publish the private beta page and installer checksum.

### Week 2: users and learning

1. Invite five qualified candidates to obtain three completed installs.
2. Run onboarding calls under the Rezonance brand.
3. Review redacted support bundles and product friction daily.
4. Fix installation/discovery blockers before adding features.
5. Add the OpenAI full-agent adapter in parallel, but do not delay initial Anthropic-key users if the deterministic and safety gates pass.

## Founder versus Codex responsibilities

| Work | Codex can execute | Founder required |
|---|---|---|
| Entitlement, local runtime, vault, installer code, provider adapters, tests | Yes | Review product boundary |
| Windows VM/GNS3 automated test scripts | Yes | Provide/authorize the Windows test host |
| Installer signing pipeline | Yes, once credentials are available | Purchase/control code-signing certificate |
| Community landing page, docs, demo script, onboarding checklist | Yes | Approve positioning and publish identity |
| Candidate research and personalized drafts | Yes | Approve recipients and send messages |
| Onboarding sessions and customer discovery | Prepare and summarize | Founder must attend |
| Provider terms and billing review | Research and flag changes | Founder/legal makes release decision |

## Go/no-go checklist

Community is GO only when:

- clean Windows install requires no developer tools;
- device and provider credentials are stored only in the Windows vault;
- all listeners are loopback-only by default;
- the 25-device cap is server-enforced and race-safe;
- Digital Twin and Shell work with no model provider;
- Rez is read-only and provider-neutral gates pass;
- OpenAI/Anthropic costs are clearly the user's responsibility;
- no consumer-subscription entitlement is implied;
- uninstall/upgrade/restart paths pass;
- the Windows/GNS3 Marcus certification is documented;
- the three-user onboarding funnel is ready before public promotion.

## Deferred until after three users

- one-click provider subscription OAuth unless explicitly supported for third-party products;
- macOS/Linux desktop installers;
- shared SaaS Community workspaces;
- Teams/Slack and monitoring webhooks for local-only users;
- automatic Netcode production writes;
- local-model support that cannot match frozen RCA safety/accuracy gates;
- NetBox, Infoblox, FortiManager, or Panorama integrations.
