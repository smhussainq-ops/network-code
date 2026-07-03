# Change Log: Arista MVP UI Rebuild

Date: 2026-07-03

## Summary

Rebuilt the UI around a clear network-as-code product flow:

```text
Home -> Setup -> Inventory -> Desired State -> Plan -> Validate -> Apply -> Evidence
```

This replaces the previous button-heavy interface with a focused MVP experience
for the Arista lab and the local Git repo.

## Backend Changes

- Added `GET /api/git/status`.
- Added `POST /api/git/setup` so the UI can initialize the current runtime
  workspace and attach the configured Git remote.
- Added `GET /api/desired-state/catalog`.
- Added `POST /api/desired-state/plan`.
- Added `POST /api/verify/intent`.
- Added `GET /api/audit/sessions`.
- Added editable UI configuration APIs:
  - `GET /api/config/ui`
  - `POST /api/config/ui`
  - `POST /api/config/ui/reset`
  - `GET /api/config/ui/history`
- Added `.netcode/ui_config.yaml` for persisted platform/UI settings.
- Added `.netcode/ui_config_history.yaml` for configuration audit history.
- Added `git_workspace_status()` helper.
- Added `setup_git_workspace()` helper.
- Added typed desired-state models for:
  - VLAN
  - Interface config
  - BGP neighbor
  - ACL rule
  - Site/device source-of-truth intent
- Added per-intent Jinja templates and validation gates.
- Added per-intent plan metadata for risk, lab apply support, and production
  lock state.
- Kept existing working APIs for:
  - Source of truth
  - Rez discovery
  - GitOps plan
  - Add VLAN intent
  - Static pipeline
  - Lab dry-run
  - Lab apply
  - Lab rollback
  - Live VLAN verification
  - Jobs and workflow evidence

## UI Changes

- Replaced the old console/journey split with one MVP product shell.
- Added left navigation:
  - Home
  - Setup
  - Inventory
  - Desired State
  - Plan
  - Validate
  - Apply
  - Drift
  - Evidence
- Rebuilt Desired State as a dynamic intent builder instead of a fixed VLAN
  form.
- Removed hardcoded visible lab defaults from the browser forms; defaults now
  load from the editable platform configuration.
- Added Setup controls for:
  - Git repo URL, branch, and commit message
  - Source-of-truth provider and paths
  - Credential profile, username, and port
  - Discovery defaults
  - Desired-state defaults
  - Workflow gates, canary size, and batch size
- Added full configuration JSON editor so every UI-consumed option can be
  changed without code edits.
- Added change-type cards for VLAN, interface, BGP, ACL, and site/device intent.
- Added dynamic form fields per selected change type.
- Added apply-gate visibility so unsupported write paths are locked in the UI.
- Added persistent outcome panel showing:
  - Expected result
  - Actual result
  - Artifact created or inspected
  - Whether device config changed
  - Next safe action
- Added setup health view for:
  - Git
  - Source of truth
  - Rez adapters
  - Arista lab
- Added inventory discovery flow using Rez.
- Added source-of-truth import from the UI.
- Added desired-state form for the Arista VLAN MVP.
- Added Terraform-style plan summary.
- Added validation gate view.
- Added apply/verify/rollback gate view.
- Added drift view.
- Added evidence tabs for YAML, generated commands, validation, lab proof, Git,
  UI configuration, audit sessions, and jobs.

## Safety Behavior

- Setup does not touch device config.
- Discovery does not touch device config.
- Plan does not touch device config.
- Static validation does not touch device config.
- Dry-run uses EOS config session and aborts it.
- Apply is locked until validation and dry-run pass.
- Apply is also locked when an intent type does not have lab write support.
- Production write remains locked for every current intent type.
- Verification is read-only.
- Rollback is available after apply.
- Every dry-run/apply/rollback command session is stored in job evidence and
  exposed through the Audit evidence tab.
- Every UI configuration save/reset is written to config history and exposed
  through the Config evidence tab.

## Tests

- Updated UI route test to assert the new MVP flow.
- Added desired-state catalog and multi-intent plan tests.
- Added audit session transcript test.
- Added direct lab-result audit transcript test for the current job storage
  shape.
- Added UI configuration persistence test.
- Added configured source-of-truth path test.
- Existing backend tests remain in place.

## Out of Scope

- Multi-vendor config push.
- Production RBAC and approvals.
- NetBox/Nautobot write integration.
- Enterprise secrets management.
- Change-window enforcement.

## 2026-07-03 User Story Repair

- Added Home user-story cards for Connect Git, Discover Devices, Build Source
  of Truth, Plan Safe Change, and Prove/Audit.
- Wired Connect Git to the new setup endpoint and the configured
  `network-code` GitHub remote.
- Changed Setup summaries so Rez adapter and containerlab readiness are shown
  as concise outcomes instead of raw backend payloads.
- Fixed Discovery story status so a successful Rez scan updates the card to
  `Discovered`.
- Fixed audit session extraction so dry-run, apply, and rollback transcripts
  from the current lab job format appear in Evidence.
- Verified in the ORB Arista lab UI at `http://127.0.0.1:8091/app`:
  Git connect passed, Rez discovery passed, plan/validation passed, dry-run
  passed, lab apply passed, live verification passed, rollback passed, and
  Evidence showed four command sessions.

## 2026-07-03 Git Change-Branch Workflow + Editable Setup (Claude)

Implemented by Claude; validation owned by Codex.

### Backend

- Added `list_git_branches()` and `create_change_branch()` to
  `netcode/gitflow.py`:
  - Branch names are validated with `git check-ref-format --branch`.
  - Creating an existing branch switches to it instead (idempotent).
  - Optional base branch supported (`git checkout -b <name> <base>`).
  - Non-repo workspaces and invalid names return honest `ok=false`
    outcomes with the exact git steps that ran — nothing crashes.
- Added `GET /api/git/branches` (current branch + local branches).
- Added `POST /api/git/branch` (`{name, base}` create-or-switch).
- `GET /api/health` now returns a UI-safe lab summary
  (`message`, `running_nodes`, `nodes`) instead of the raw
  `clab inspect` stdout — defense-in-depth so no future UI change can
  re-expose a raw dump.

### UI (Setup Step 1 is now a workflow, not a status card)

- Editable Repo URL and Base branch fields inline on the Git card
  (prefilled from config/status; used by Connect).
- Working-branch indicator, New change branch input with a suggested
  `change/<site>-<change-type>` name, existing-branch dropdown, and
  Create change branch / Switch buttons.
- Git commands block now shows the branch-first flow:
  `git checkout -b change/... && git add && git commit && git push -u`.
- Steps 2-4 (source of truth, read adapters, lab) render structured
  stat chips instead of text blobs; removed the Rez filesystem path
  from user-facing copy.
- Connect button stays enabled as "Update Git connection" after
  connect so the remote can be changed from the UI.
- Story 01 card now shows the active branch
  (`Git connected · change/...`).
- Asset version bumped to `mvp8`.

### Hygiene

- `.gitignore` now ignores macOS `._*` AppleDouble files.
- ORB deploys should use `COPYFILE_DISABLE=1 tar ...` so runtime
  `git status` stays clean.

### Tests (33 passing)

- `test_git_branch_endpoint_creates_and_switches_change_branch`:
  blocked before repo exists, create, switch back, idempotent
  re-create, invalid name rejected, empty name rejected, branch
  listing.
- `test_health_endpoint_returns_lab_summary_not_raw_dump`.
- `test_lab_summary_shapes_clab_output_into_counts`.

## 2026-07-03 Guided User Stories Build (Claude implements, Codex validates)

Rebuilt the product around the agreed 5 user stories and the rule:
**every screen must answer "what decision can the engineer make now?"**

### The 5 stories (Home cards, live progress)

1. Get ready — Setup readiness gates (x/4)
2. Bring devices under management — discovery/import
3. Make a safe change — the 9-step golden path (see below)
4. Prove it — one change record per change
5. Catch drift — live vs intent, reconcile starts a new change

### Story 3 golden path (Git woven in, not bolted on)

Declare → Branch → Plan → Validate → Dry-run → Commit → Apply →
Verify → Push. A progress rail shows these steps (done/next/todo) on
the Desired State, Plan, Validate, and Apply views.

- Branch step lives in Desired State: suggested
  `change/<site>-<type>` name (from plan metadata), create/switch.
- Commit + Push live in Apply ("Send for review"): editable commit
  message, honest push result, PR-ready summary from the GitOps plan.
- New endpoints: `POST /api/git/commit` (stage+commit with identity
  fallback, `nothing_to_commit` idempotency), `POST /api/git/push`
  (real attempt; credential failures reported honestly with the
  command to run). Both record `git_commit`/`git_push` workflow
  events on the change when `change_id` is supplied.

### Plan shows risk before any device contact

- Blast radius chips: affected devices and objects.
- Rollback plan BEFORE apply: exact inverse commands + confidence
  level with reason (high for VLAN, medium for interface/BGP/ACL,
  none for inventory records).
- Pre/post checks per change type, honest about which execute live
  (`executable: false` = definition only, never fake green).
- Plan metadata now includes `blast_radius`, `rollback`, `checks`,
  and `suggested_branch` (netcode/intent_utils.py).

### Setup = 4 readiness gates

Gate 1 Git (editable repo URL/base branch), Gate 2 Source of truth
(active provider + credential provenance stated), Gate 3 Read access,
Gate 4 Safe test target (runner labeled: "this runtime — your browser
never touches devices"). Each gate is pass/fail with one fix action.
All green unlocks "Start a change". Config editor moved behind
Advanced. Vendor lists, node counts, and template counts removed —
they answered no decision.

### Evidence = one change record

`GET /api/change/{id}/record` packages Request / Plan / Safety /
Lab proof / Apply proof / Verification / Rollback / Git record /
Artifact manifest per change. The Evidence view renders it readably;
raw artifact tabs moved behind Advanced.

### Hygiene

- Bootstrapped workspaces now seed a `.gitignore` (create-only, never
  overwritten by --force) so `.netcode/` state is never committed to
  change branches.
- Asset version mvp9.

### Tests (35 passing)

- `test_git_commit_and_push_endpoints_report_honestly`.
- `test_change_record_packages_request_plan_safety_git_and_manifest`
  (blast radius, rollback commands + confidence, pre-checks,
  suggested branch, manifest existence, git_commit event, 404).
- UI route test updated to the new story board and screens.
