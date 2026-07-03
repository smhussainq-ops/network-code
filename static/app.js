let currentIntentPath = "";
let currentChangeId = "";
let safetyPassed = false;
let dryRunPassed = false;
let applyPassed = false;
let applying = false;
let journeyCurrentStep = "define";
let journeyPipelineData = null;
let journeyDefinedRequest = null;
let lastDiscoveryCandidate = null;
const journeyArtifacts = {};
const journeyCompletedSteps = new Set();
const journeyFailedSteps = new Set();

const $ = (id) => document.getElementById(id);

const ACTIONS = {
  "check-safety": {
    title: "Clicked: Check safety",
    expected:
      "Create intent YAML, render EOS config from Jinja, run 7 static checks, write report artifacts, and keep device writes locked.",
    running: "Creating the request artifact, rendering the candidate config, and running policy validation. No device is contacted.",
  },
  "test-candidate": {
    title: "Clicked: Test candidate",
    expected:
      "Open an EOS config session, load the candidate config, collect the session diff, abort the session, and unlock apply only if the device accepts it.",
    running: "Connecting to the lab device and testing the config in an abortable config session. No commit should happen.",
  },
  "apply-change": {
    title: "Clicked: Apply in lab",
    expected:
      "Commit the already-tested candidate, verify the VLAN exists on the device, and record the job evidence.",
    running: "Committing the candidate to the lab device, then verifying the VLAN state from the device.",
  },
  rollback: {
    title: "Clicked: Rollback lab VLAN",
    expected:
      "Commit a compensating no-vlan change, verify the VLAN is absent, and record rollback evidence.",
    running: "Removing the lab VLAN through a config session and verifying it is gone.",
  },
  "refresh-platform": {
    title: "Clicked: Refresh status",
    expected: "Check API health, lab reachability, latest jobs, and the 15 platform capabilities.",
    running: "Refreshing runtime status and platform capability evidence.",
  },
  "show-source-of-truth": {
    title: "Clicked: Source of truth",
    expected: "Show the inventory, policies, templates, sites, groups, platforms, and known subnets the platform trusts.",
    running: "Loading the source-of-truth snapshot from the local provider.",
  },
  "show-adapter-matrix": {
    title: "Clicked: Adapter matrix",
    expected: "Show which vendors have Rez read support, which have execution adapters, and which writes remain locked.",
    running: "Loading Rez health, Rez platforms, and Netcode execution adapter capabilities.",
  },
  "run-discovery": {
    title: "Clicked: Discover device",
    expected: "Use Rez multi-vendor read drivers to identify a device, collect state, and create an import-ready source-of-truth record.",
    running: "Trying the selected Rez vendor driver, or auto-detecting through Rez if no vendor was selected. No config writes are allowed.",
  },
  "import-discovered-device": {
    title: "Clicked: Save discovered device",
    expected: "Write the reviewed discovery candidate into local YAML source of truth without storing the discovery password.",
    running: "Updating the local inventory file with the discovered device record.",
  },
  "show-workflow-rules": {
    title: "Clicked: Workflow rules",
    expected: "Show every workflow state with allowed actions, blocked actions, and required evidence.",
    running: "Loading the platform state-machine contract.",
  },
  "show-gitops-plan": {
    title: "Clicked: GitOps plan",
    expected: "Show the branch, commit, PR, and artifact plan for the current validated intent.",
    running: "Building the GitOps promotion plan.",
  },
  "show-conformance": {
    title: "Clicked: Adapter conformance",
    expected: "Show read/write contract status for every supported or planned vendor adapter.",
    running: "Loading adapter conformance contracts.",
  },
  "show-verification-catalog": {
    title: "Clicked: Verification catalog",
    expected: "Show vendor-neutral verification checks available through Rez live state.",
    running: "Loading verification check catalog.",
  },
  "run-drift-check": {
    title: "Clicked: Drift check",
    expected: "Compare intended VLAN state against live Rez state and classify drift.",
    running: "Collecting state and comparing intended versus live VLAN state.",
  },
  "show-scale-plan": {
    title: "Clicked: Scale plan",
    expected: "Show canary, batch, lock, retry, and pause controls for scaled rollout.",
    running: "Building rollout plan from current source-of-truth devices.",
  },
  "ask-assistant": {
    title: "Clicked: AI assistant",
    expected: "Explain risk, missing evidence, or draft proposed intent without executing any change.",
    running: "Building guarded assistant response. No network action is allowed from this path.",
  },
  "show-jobs": {
    title: "Clicked: Show job history",
    expected: "Read durable job records so the last actions and their evidence are visible.",
    running: "Loading job records from the platform store.",
  },
  "show-device-state": {
    title: "Clicked: Show device state",
    expected: "Use the Rez state adapter bridge to collect current device state when available.",
    running: "Calling the Rez adapter bridge for the selected device.",
  },
  "verify-vlan-state": {
    title: "Clicked: Verify VLAN state",
    expected: "Collect live device state through Rez and prove whether the requested VLAN exists.",
    running: "Collecting live state and running the VLAN verification contract.",
  },
  "toggle-details": {
    title: "Clicked: Show technical details",
    expected: "Reveal or hide raw YAML, generated config, validation checks, lab proof, and job records.",
    running: "Changing the evidence view. No backend call is needed.",
  },
  "select-detail": {
    title: "Clicked: Evidence tab",
    expected: "Switch the visible evidence pane without changing network or platform state.",
    running: "Showing the selected evidence pane.",
  },
};

const WORKFLOW_STATES = [
  "draft",
  "intent_created",
  "rendered",
  "validated",
  "state_collected",
  "dry_run_passed",
  "approval_required",
  "approved",
  "applying",
  "verified",
  "completed",
  "rollback_available",
  "rolling_back",
  "rolled_back",
  "failed",
  "blocked",
];

const JOURNEY_STEPS = [
  {
    id: "define",
    number: "01",
    title: "Define Change",
    button: "Use this request",
    copy: "Choose the network change you want to make before the platform inspects or creates artifacts.",
    creates: "Confirms the request from the form: change type, site, target device, VLAN, name, subnet, purpose, and requester.",
    why: "Network as code starts with user intent. The platform should not assume the engineer wants the default demo change.",
    next: "Click Step 02 to check source of truth for this request.",
  },
  {
    id: "source",
    number: "02",
    title: "Source of Truth",
    button: "Inspect source of truth",
    copy: "First, confirm the platform knows the device, vendor, policies, and template before it builds anything.",
    creates: "Checks the trusted network data: devices, sites, vendor type, known subnets, policies, and templates.",
    why: "This prevents changes to unknown devices or changes built from missing policy data.",
    next: "Click Step 03 to create the intent YAML.",
  },
  {
    id: "intent",
    number: "03",
    title: "Intent YAML",
    button: "Create intent YAML",
    copy: "Turn the form into a simple change request file that can be reviewed before config is generated.",
    creates: "Creates a YAML file that says what change is wanted, which device is targeted, and who requested it.",
    why: "The engineer reviews the request before thinking about vendor commands.",
    next: "Click Step 04 to set up or inspect Git review.",
  },
  {
    id: "gitops",
    number: "04",
    title: "Git Setup",
    button: "Check Git setup",
    copy: "Check whether this platform folder is a Git repo and show the exact commands for reviewable network changes.",
    creates: "Inspects Git status and produces setup, branch, add, and commit commands for the current intent.",
    why: "Git gives the network team history, review, rollback context, and proof of exactly what changed.",
    next: "Click Step 05 to inspect the Jinja template.",
  },
  {
    id: "template",
    number: "05",
    title: "Jinja Template",
    button: "Inspect Jinja template",
    copy: "Inspect the template that turns intent data into vendor config.",
    creates: "Reads the real Arista add-VLAN Jinja template used by the renderer.",
    why: "Templates make config consistent so engineers are not hand-typing the same commands differently every time.",
    next: "Click Step 06 to inspect the generated candidate config.",
  },
  {
    id: "candidate",
    number: "06",
    title: "Candidate Config",
    button: "Inspect candidate config",
    copy: "Inspect the actual EOS config the platform generated from the request and template.",
    creates: "Reads the rendered .eos candidate config and the template variables used to build it.",
    why: "You see the commands before they ever touch a switch.",
    next: "Click Step 07 to inspect policy validation.",
  },
  {
    id: "validation",
    number: "07",
    title: "Static Validation",
    button: "Inspect validation checks",
    copy: "Check the request and generated config against policy before a device is contacted.",
    creates: "Reads the static validation report and each pass/fail guardrail.",
    why: "Unsafe requests should stop here, before a lab or production device sees any command.",
    next: "Click Step 08 to test on the lab switch without committing.",
  },
  {
    id: "dryrun",
    number: "08",
    title: "Lab Dry-Run",
    button: "Test candidate in lab",
    copy: "Send the candidate to an EOS config session, prove the switch accepts it, then abort.",
    creates: "Creates a dry-run job, command transcript, and session diff. No persistent change is made.",
    why: "The real device must accept the config before apply is unlocked.",
    next: "Click Step 09 to apply and verify in the lab.",
  },
  {
    id: "apply",
    number: "09",
    title: "Apply + Verify",
    button: "Apply and verify in lab",
    copy: "Commit only the already-tested candidate in the lab, then verify the VLAN exists.",
    creates: "Creates an apply job, commit transcript, and post-change verification result.",
    why: "A change is not done until the platform proves the device reached the intended state.",
    next: "Click Step 10 to prove rollback.",
  },
  {
    id: "rollback",
    number: "10",
    title: "Rollback",
    button: "Rollback lab change",
    copy: "Remove the lab VLAN and verify the switch returns to the expected state.",
    creates: "Creates a rollback job, rollback command transcript, and absence verification result.",
    why: "A safe platform must prove how it recovers, not just how it applies.",
    next: "Click Step 11 to inspect the evidence package.",
  },
  {
    id: "evidence",
    number: "11",
    title: "Evidence Package",
    button: "Inspect evidence package",
    copy: "Collect the proof: request, validation, commands, jobs, workflow events, and reports.",
    creates: "Reads the durable change record, job records, Git plan, reports, and workflow history.",
    why: "This is what you can show in review: what was requested, what ran, what passed, and what was rolled back.",
    next: "Open the Change Console when you are ready to operate the workflow.",
  },
];

const JOURNEY_STEP_IDS = JOURNEY_STEPS.map((step) => step.id);

function formatJson(data) {
  return JSON.stringify(data, null, 2);
}

function formPayload() {
  const form = new FormData($("change-form"));
  return {
    site: form.get("site"),
    device_id: form.get("device_id"),
    vlan_id: Number(form.get("vlan_id")),
    name: form.get("name"),
    subnet: form.get("subnet"),
    purpose: form.get("purpose"),
    requested_by: form.get("requested_by"),
    pci_reachable: form.get("pci_reachable") === "on",
  };
}

function syncRequestedChange() {
  const payload = formPayload();
  $("requested-change").textContent = `Add VLAN ${payload.vlan_id} to ${payload.device_id}`;
}

function requestChanged() {
  currentIntentPath = "";
  currentChangeId = "";
  safetyPassed = false;
  dryRunPassed = false;
  applyPassed = false;
  applying = false;
  journeyPipelineData = null;
  journeyDefinedRequest = null;
  journeyCompletedSteps.clear();
  journeyFailedSteps.clear();
  Object.keys(journeyArtifacts).forEach((key) => delete journeyArtifacts[key]);
  journeyCurrentStep = "define";
  syncRequestedChange();
  resetProofGates();
  setCard("card-safety", "current");
  setCard("card-lab", "locked");
  setCard("card-apply", "locked");
  $("safety-verdict").textContent = "Not checked";
  $("device-proof").textContent = "Required";
  $("audit-trail").textContent = "Pending";
  $("detail-commands").textContent = "";
  setOutcome(
    "Request changed. Re-check safety.",
    "Any request edit invalidates previous proof. Run safety checks again before lab actions."
  );
  setLivePanel({
    title: "Request changed",
    status: "info",
    expected: "Re-run safety checks before lab actions. Previous proof no longer applies to the edited request.",
    actual: "Proof gates were reset and device actions were locked.",
    evidence: "No device commands were sent for this edit.",
    commands: "No device commands were sent.",
  });
  renderLocks();
  renderJourneyStep();
}

function renderDiscoveryImport() {
  const button = $("import-discovered-device");
  if (!button) return;
  button.disabled = !lastDiscoveryCandidate;
}

function timestamp() {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date());
}

function lockStateText() {
  const dryRun = safetyPassed ? "Dry-run unlocked" : "Dry-run locked";
  const apply = safetyPassed && dryRunPassed && !applying ? "Apply unlocked" : "Apply locked";
  const rollback = applyPassed && !applying ? "Rollback unlocked" : "Rollback locked";
  return `${dryRun}. ${apply}. ${rollback}.`;
}

function updateLockState() {
  $("live-lock-state").textContent = lockStateText();
}

function setLiveState(status) {
  const pill = $("live-action-state");
  pill.className = "";
  pill.classList.add(`state-${status}`);
  const labels = {
    idle: "Idle",
    running: "Running",
    pass: "Passed",
    fail: "Failed",
    info: "Info",
  };
  pill.textContent = labels[status] || status;
}

function setLivePanel({ title, status, expected, actual, evidence, commands = "No device commands were sent." }) {
  $("live-action-title").textContent = title;
  $("live-action-expected").textContent = expected;
  $("live-action-actual").textContent = actual;
  $("live-action-evidence").textContent = evidence;
  $("live-action-commands").textContent = commands;
  setLiveState(status);
  updateLockState();
}

function appendJournalEntry({ title, status, expected, actual, evidence, commands = "No device commands were sent." }) {
  const journal = $("live-journal");
  const empty = journal.querySelector(".journal-empty");
  if (empty) empty.remove();

  const entry = document.createElement("article");
  entry.className = `journal-entry ${status}`;

  const header = document.createElement("div");
  header.className = "journal-entry-header";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const time = document.createElement("span");
  time.textContent = timestamp();
  header.appendChild(heading);
  header.appendChild(time);

  const state = document.createElement("span");
  state.className = "journal-state";
  state.textContent = status.toUpperCase();

  const expectedLine = document.createElement("p");
  expectedLine.textContent = `Expected: ${expected}`;
  const actualLine = document.createElement("p");
  actualLine.textContent = `Actual: ${actual}`;
  const evidenceLine = document.createElement("p");
  evidenceLine.textContent = `Evidence: ${evidence}`;
  const commandsLine = document.createElement("p");
  commandsLine.className = "journal-commands";
  commandsLine.textContent = `Commands:\n${commands}`;

  entry.appendChild(header);
  entry.appendChild(state);
  entry.appendChild(expectedLine);
  entry.appendChild(actualLine);
  entry.appendChild(evidenceLine);
  entry.appendChild(commandsLine);
  journal.prepend(entry);

  while (journal.querySelectorAll(".journal-entry").length > 12) {
    journal.querySelector(".journal-entry:last-child").remove();
  }
}

function startAction(actionKey) {
  const action = ACTIONS[actionKey];
  setLivePanel({
    title: action.title,
    status: "running",
    expected: action.expected,
    actual: action.running,
    evidence: "Waiting for platform response.",
    commands: "Waiting for platform response.",
  });
  appendJournalEntry({
    title: action.title,
    status: "running",
    expected: action.expected,
    actual: action.running,
    evidence: "Started.",
    commands: "Waiting for platform response.",
  });
}

function completeAction(actionKey, { status = "pass", actual, evidence, commands = "No device commands were sent." }) {
  const action = ACTIONS[actionKey];
  setLivePanel({
    title: action.title,
    status,
    expected: action.expected,
    actual,
    evidence,
    commands,
  });
  appendJournalEntry({
    title: action.title,
    status,
    expected: action.expected,
    actual,
    evidence,
    commands,
  });
}

function failAction(actionKey, message) {
  completeAction(actionKey, {
    status: "fail",
    actual: message,
    evidence: "The platform stopped at this step. No later locked action was unlocked.",
  });
}

function clearJournal() {
  $("live-journal").innerHTML =
    '<article class="journal-empty">Every click will be recorded here with expected outcome, actual result, and evidence.</article>';
  setLivePanel({
    title: "Waiting for a click",
    status: "idle",
    expected: "Click an action to see exactly what should happen before anything changes.",
    actual: "No action has run yet.",
    evidence: "Evidence appears here as soon as the platform has it.",
    commands: "No device commands have been sent.",
  });
}

function commandListFromText(config) {
  return String(config || "")
    .split("\n")
    .map((line) => line.trimEnd())
    .filter((line) => line.trim());
}

function labTranscript(result) {
  const evidence = result?.evidence || {};
  return evidence.transcript || evidence.session?.transcript || [];
}

function commandListFromLabResult(result) {
  return labTranscript(result)
    .map((entry) => entry.command)
    .filter(Boolean);
}

function formatCommandList(commands, empty = "No device commands were sent.") {
  if (!commands.length) return empty;
  return commands.map((command) => `$ ${command}`).join("\n");
}

function commandTranscriptText(data) {
  const result = data.result || data;
  const commands = commandListFromLabResult(result);
  const heading =
    result.action === "dry-run"
      ? `# Commands loaded into EOS config session ${result.session_name || ""} and aborted`
      : result.action === "apply"
        ? `# Commands committed in EOS config session ${result.session_name || ""}`
        : result.action === "rollback"
          ? `# Rollback commands committed in EOS config session ${result.session_name || ""}`
          : "# Device commands";
  return `${heading.trim()}\n${formatCommandList(commands)}`;
}

function apiErrorMessage(data, fallback) {
  if (!data || data.detail == null) return fallback;
  return typeof data.detail === "string" ? data.detail : formatJson(data.detail);
}

async function getJson(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok) throw new Error(apiErrorMessage(data, response.statusText));
  return data;
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(apiErrorMessage(data, response.statusText));
  return data;
}

function setCard(id, state) {
  const card = $(id);
  card.classList.remove("locked", "current", "pass", "fail");
  card.classList.add(state);
}

function renderLocks() {
  $("run-dry-run").disabled = !safetyPassed;
  $("run-apply").disabled = !(safetyPassed && dryRunPassed) || applying;
  $("run-rollback").disabled = !applyPassed || applying;
  renderDiscoveryImport();
  updateLockState();
}

function setOutcome(title, copy) {
  $("outcome-title").textContent = title;
  $("outcome-copy").textContent = copy;
}

function setAssurance(name, state, value, detail) {
  const valueEl = $(`assurance-${name}`);
  const detailEl = $(`assurance-${name}-detail`);
  if (!valueEl || !detailEl) return;
  const card = valueEl.closest(".assurance-card");
  card.classList.remove("state-ready", "state-pass", "state-current", "state-running", "state-locked", "state-waiting", "state-fail");
  card.classList.add(`state-${state}`);
  valueEl.textContent = value;
  detailEl.textContent = detail;
}

function resetProofGates() {
  setAssurance("policy", "waiting", "Not checked", "Static validation must pass.");
  setAssurance("lab", "locked", "Required", "Dry-run required before apply.");
  setAssurance("commands", "waiting", "None sent", "Candidate commands appear first.");
  setAssurance("audit", "waiting", "Pending", "Jobs and reports will be recorded.");
}

function setDetailsVisible(visible, log = false) {
  $("technical-details").classList.toggle("hidden", !visible);
  $("toggle-details").textContent = visible ? "Hide technical details" : "Show technical details";
  if (log) {
    ACTIONS["toggle-details"].title = visible ? "Clicked: Show technical details" : "Clicked: Hide technical details";
    completeAction("toggle-details", {
      status: "info",
      actual: visible ? "Technical evidence is now visible." : "Technical evidence is now hidden.",
      evidence: "No network or backend state changed.",
    });
  }
}

function updateChecks(checks) {
  const container = $("detail-checks");
  container.innerHTML = "";
  checks.forEach((check) => {
    const row = document.createElement("div");
    row.className = `check-row ${check.status}`;
    row.innerHTML = `<strong>${check.status.toUpperCase()}: ${check.title}</strong><p>${check.message}</p>`;
    container.appendChild(row);
  });
}

function renderCapabilities(capabilities) {
  const deliverables = capabilities.deliverables || [];
  const container = $("platform-checklist");
  container.innerHTML = "";
  $("platform-count").textContent = `${deliverables.length}/15 ready`;

  deliverables.forEach((item, index) => {
    const card = document.createElement("article");
    card.className = "capability-item";

    const number = document.createElement("span");
    number.textContent = String(index + 1).padStart(2, "0");

    const body = document.createElement("div");
    const title = document.createElement("h4");
    title.textContent = item.name;
    const meaning = document.createElement("p");
    meaning.textContent = item.simple_meaning;

    body.appendChild(title);
    body.appendChild(meaning);
    card.appendChild(number);
    card.appendChild(body);
    container.appendChild(card);
  });
}

function updateFromPipeline(data) {
  currentIntentPath = data.intent_path;
  currentChangeId = data.change?.id || "";
  journeyPipelineData = data;
  const pipeline = data.pipeline;
  safetyPassed = pipeline.status === "pass";
  dryRunPassed = false;
  applyPassed = false;
  syncRequestedChange();

  $("safety-verdict").textContent = safetyPassed ? "Passed" : "Failed";
  $("device-proof").textContent = "Required before apply";
  $("audit-trail").textContent = "Safety report created";
  $("detail-intent").textContent = pipeline.intent_yaml;
  $("detail-config").textContent = pipeline.render.config;
  const candidateCommands = commandListFromText(pipeline.render.config);
  $("detail-commands").textContent = `# Candidate commands only. No device commands were sent during safety checks.\n${formatCommandList(candidateCommands)}`;
  $("detail-jobs").textContent = formatJson(pipeline.artifacts || {});
  $("detail-workflow").textContent = formatJson(data.workflow || {});
  updateChecks(pipeline.validation.checks);

  setCard("card-safety", safetyPassed ? "pass" : "fail");
  setCard("card-lab", safetyPassed ? "current" : "locked");
  setCard("card-apply", "locked");
  setAssurance(
    "policy",
    safetyPassed ? "pass" : "fail",
    safetyPassed ? "7 checks passed" : "Blocked",
    safetyPassed ? "Intent, target, subnet, segmentation, and render scope passed." : "Device actions remain locked."
  );
  setAssurance(
    "lab",
    safetyPassed ? "current" : "locked",
    safetyPassed ? "Dry-run next" : "Locked",
    safetyPassed ? "Apply stays locked until EOS accepts the candidate." : "Lab action blocked by policy."
  );
  setAssurance("commands", "current", "Candidate ready", "Generated commands are visible before device contact.");
  setAssurance("audit", "ready", "Report written", "Intent, rendered config, and validation report are stored.");

  setOutcome(
    safetyPassed ? "The request is safe to test." : "The request is blocked.",
    safetyPassed
      ? "Policy checks passed. The platform generated a candidate config, but device writes remain locked until lab dry-run proof passes."
      : "One or more policy checks failed. No device action is allowed."
  );
  renderLocks();
  const renderedLines = pipeline.render.config.split("\n").filter((line) => line.trim()).length;
  completeAction("check-safety", {
    status: safetyPassed ? "pass" : "fail",
    actual: safetyPassed
      ? `${pipeline.validation.checks.length}/${pipeline.validation.checks.length} checks passed. Candidate config generated.`
      : "One or more checks failed. Device actions remain locked.",
    evidence: `Intent: ${data.intent_path}. Config lines: ${renderedLines}. Report: ${pipeline.artifacts?.report_markdown_path || "not written"}.`,
    commands: `No device commands were sent.\nCandidate config:\n${formatCommandList(candidateCommands)}`,
  });
}

function updateFromLabResult(data) {
  const result = data.result || data;
  const ok = Boolean(data.ok || result.status === "pass");
  $("detail-lab").textContent = formatJson(data);
  $("detail-commands").textContent = commandTranscriptText(data);
  if (data.workflow) {
    $("detail-workflow").textContent = formatJson(data.workflow);
  }
  if (data.job) {
    $("audit-trail").textContent = `${data.job.action}: ${data.job.status}`;
    $("detail-jobs").textContent = formatJson(data.job);
  }

  if (result.action === "dry-run") {
    dryRunPassed = ok;
    $("device-proof").textContent = ok ? "Dry-run passed" : "Dry-run failed";
    setCard("card-lab", ok ? "pass" : "fail");
    setCard("card-apply", ok ? "current" : "locked");
    setAssurance(
      "lab",
      ok ? "pass" : "fail",
      ok ? "Dry-run passed" : "Dry-run failed",
      ok ? "EOS accepted the candidate and the session was aborted." : "Apply remains locked."
    );
    setAssurance("commands", ok ? "pass" : "fail", ok ? "Abort transcript" : "Rejected", ok ? "Device command transcript includes abort." : "Review command transcript.");
    setAssurance("audit", ok ? "ready" : "fail", data.job ? `Job ${data.job.status}` : "Recorded", data.job ? `${data.job.action} evidence stored.` : "Lab evidence stored.");
    setOutcome(
      ok ? "The change is proven and ready for controlled apply." : "The device rejected the candidate.",
      ok
        ? "The lab device accepted the candidate in a config session and the session was aborted. Apply is now unlocked for the lab."
        : "Apply remains locked because the dry-run did not pass."
    );
    completeAction("test-candidate", {
      status: ok ? "pass" : "fail",
      actual: result.message || (ok ? "Dry-run passed." : "Dry-run failed."),
      evidence: data.job
        ? `Job ${data.job.id}: ${data.job.action} ${data.job.status}. Config session: ${result.session_name || "not reported"}.`
        : `Config session: ${result.session_name || "not reported"}.`,
      commands: formatCommandList(commandListFromLabResult(result)),
    });
  } else if (result.action === "apply") {
    applyPassed = ok;
    $("device-proof").textContent = ok ? "Applied and verified" : "Apply failed";
    setCard("card-apply", ok ? "pass" : "fail");
    setAssurance(
      "lab",
      ok ? "pass" : "fail",
      ok ? "Applied + verified" : "Apply failed",
      ok ? "VLAN presence was verified after commit." : "Review proof before another action."
    );
    setAssurance("commands", ok ? "pass" : "fail", ok ? "Commit transcript" : "Commit failed", ok ? "Device commands include explicit commit." : "Review command transcript.");
    setAssurance("audit", ok ? "ready" : "fail", data.job ? `Job ${data.job.status}` : "Recorded", data.job ? `${data.job.action} evidence stored.` : "Apply evidence stored.");
    setOutcome(
      ok ? "The lab change was applied and verified." : "The apply did not complete safely.",
      ok
        ? "The platform recorded the job, committed the candidate, and verified the VLAN on the device."
        : "Review device proof and use rollback if needed."
    );
    completeAction("apply-change", {
      status: ok ? "pass" : "fail",
      actual: result.message || (ok ? "Apply passed." : "Apply failed."),
      evidence: data.job
        ? `Job ${data.job.id}: ${data.job.action} ${data.job.status}. Config session: ${result.session_name || "not reported"}.`
        : `Config session: ${result.session_name || "not reported"}.`,
      commands: formatCommandList(commandListFromLabResult(result)),
    });
  } else if (result.action === "rollback") {
    applyPassed = false;
    $("device-proof").textContent = ok ? "Rolled back and verified" : "Rollback failed";
    setCard("card-apply", ok ? "current" : "fail");
    setAssurance(
      "lab",
      ok ? "pass" : "fail",
      ok ? "Rollback verified" : "Rollback failed",
      ok ? "VLAN absence was verified after rollback." : "Review rollback proof."
    );
    setAssurance("commands", ok ? "pass" : "fail", ok ? "Rollback commit" : "Rollback failed", ok ? "Rollback commands include explicit commit." : "Review command transcript.");
    setAssurance("audit", ok ? "ready" : "fail", data.job ? `Job ${data.job.status}` : "Recorded", data.job ? `${data.job.action} evidence stored.` : "Rollback evidence stored.");
    setOutcome(
      ok ? "The lab change was rolled back and verified." : "The rollback did not complete safely.",
      ok
        ? "The platform removed the lab VLAN, verified it is absent, and recorded rollback evidence."
        : "Review the rollback evidence before taking another action."
    );
    completeAction("rollback", {
      status: ok ? "pass" : "fail",
      actual: result.message || (ok ? "Rollback passed." : "Rollback failed."),
      evidence: data.job
        ? `Job ${data.job.id}: ${data.job.action} ${data.job.status}. Config session: ${result.session_name || "not reported"}.`
        : `Config session: ${result.session_name || "not reported"}.`,
      commands: formatCommandList(commandListFromLabResult(result)),
    });
  }
  renderLocks();
}

async function checkSafety() {
  startAction("check-safety");
  resetProofGates();
  setAssurance("policy", "running", "Checking", "Building candidate and running policy validation.");
  setBusy(true);
  try {
    const data = await postJson("/api/wizard/add-vlan", formPayload());
    updateFromPipeline(data);
  } catch (error) {
    safetyPassed = false;
    dryRunPassed = false;
    $("safety-verdict").textContent = "Failed";
    setCard("card-safety", "fail");
    setAssurance("policy", "fail", "Blocked", error.message);
    setAssurance("lab", "locked", "Locked", "Static validation did not pass.");
    setAssurance("commands", "waiting", "None sent", "No device commands were sent.");
    setOutcome("The request is blocked.", error.message);
    failAction("check-safety", error.message);
    renderLocks();
  } finally {
    setBusy(false);
  }
}

async function runLabAction(action) {
  const actionKey = action === "dry-run" ? "test-candidate" : action === "apply" ? "apply-change" : "rollback";
  if (!currentIntentPath) {
    setOutcome("Run safety checks first.", "The platform needs a validated request before it can test or apply anything.");
    failAction(actionKey, "Run safety checks first. The platform needs a validated request before it can test or apply anything.");
    return;
  }
  startAction(actionKey);
  applying = action === "apply" || action === "rollback";
  setAssurance("lab", "running", action === "dry-run" ? "Dry-run running" : action === "apply" ? "Applying" : "Rolling back", "Waiting for EOS proof.");
  setAssurance("commands", "running", "Sending commands", "Command transcript will appear when the device responds.");
  renderLocks();
  setBusy(true);
  try {
    const data = await postJson(`/api/lab/${action}`, {
      intent_path: currentIntentPath,
      device_id: formPayload().device_id,
      change_id: currentChangeId || null,
    });
    updateFromLabResult(data);
  } catch (error) {
    if (action === "dry-run") {
      $("device-proof").textContent = "Dry-run failed";
      setCard("card-lab", "fail");
    } else if (action === "rollback") {
      $("device-proof").textContent = "Rollback failed";
      setCard("card-apply", "fail");
    }
    setOutcome("The device proof failed.", error.message);
    failAction(actionKey, error.message);
    renderLocks();
  } finally {
    applying = false;
    setBusy(false);
  }
}

function setBusy(isBusy) {
  document.querySelectorAll("button").forEach((button) => {
    button.disabled = isBusy;
  });
  if (!isBusy) {
    document.querySelectorAll("button").forEach((button) => {
      button.disabled = false;
    });
    renderLocks();
  }
}

async function refreshPlatform({ log = true } = {}) {
  syncRequestedChange();
  if (log) startAction("refresh-platform");
  try {
    const [health, capabilities, jobs, source, adapters] = await Promise.all([
      getJson("/api/health"),
      getJson("/api/platform/capabilities"),
      getJson("/api/jobs"),
      getJson("/api/source-of-truth"),
      getJson("/api/adapters"),
    ]);
    $("runtime-pill").textContent = health.lab?.ok ? "Lab reachable" : "Local validation only";
    setAssurance(
      "source",
      source.ok ? "ready" : "fail",
      source.ok ? `${source.summary?.device_count || 0} devices` : "Unavailable",
      source.ok ? `${source.provider}: ${source.summary?.site_count || 0} sites, ${source.summary?.template_count || 0} templates.` : "Source-of-truth provider did not respond."
    );
    if (!safetyPassed && !dryRunPassed && !applyPassed) {
      setAssurance(
        "audit",
        jobs.jobs?.length ? "ready" : "waiting",
        jobs.jobs?.length ? `${jobs.jobs.length} jobs` : "Pending",
        jobs.jobs?.length ? "Existing job evidence is available." : "Jobs and reports will be recorded."
      );
    }
    renderCapabilities(capabilities);
    populateDiscoveryPlatforms(adapters.state_adapters?.rez?.platforms || []);
    $("detail-source").textContent = formatJson(source);
    $("detail-adapters").textContent = formatJson(adapters);
    $("detail-jobs").textContent = formatJson({
      latest_jobs: jobs.jobs?.slice(0, 5) || [],
      platform_deliverables: capabilities.deliverables,
    });
    if (log) {
      completeAction("refresh-platform", {
        status: "pass",
        actual: health.lab?.ok ? "API is healthy and the lab is reachable." : "API is healthy. Lab is not reachable, so device proof is unavailable.",
        evidence: `${capabilities.deliverables?.length || 0}/15 capabilities loaded. Latest jobs shown in technical details.`,
      });
    }
  } catch (error) {
    $("runtime-pill").textContent = "Runtime unavailable";
    $("platform-count").textContent = "Unavailable";
    $("platform-checklist").textContent = error.message;
    $("detail-jobs").textContent = error.message;
    if (log) failAction("refresh-platform", error.message);
  }
}

async function showSourceOfTruth() {
  startAction("show-source-of-truth");
  try {
    const data = await getJson("/api/source-of-truth");
    $("detail-source").textContent = formatJson(data);
    setDetailsVisible(true);
    selectDetail("source", false);
    completeAction("show-source-of-truth", {
      status: "pass",
      actual: `${data.summary?.device_count || 0} devices, ${data.summary?.site_count || 0} sites, ${data.summary?.platform_count || 0} platforms loaded.`,
      evidence: `Inventory: ${data.files?.inventory || "unknown"}. Policies: ${data.files?.policies || "unknown"}.`,
    });
  } catch (error) {
    failAction("show-source-of-truth", error.message);
  }
}

async function showAdapterMatrix() {
  startAction("show-adapter-matrix");
  try {
    const [adapters, rezHealth, rezPlatforms] = await Promise.all([
      getJson("/api/adapters"),
      getJson("/api/adapters/rez/health"),
      getJson("/api/adapters/rez/platforms"),
    ]);
    const adapterMatrix = adapters.adapter_matrix || [];
    $("detail-adapters").textContent = formatJson({
      adapter_matrix: adapterMatrix,
      execution_adapters: adapters.execution_adapters,
      rez: {
        health: rezHealth,
        platforms: rezPlatforms.platforms || [],
        error: rezHealth.error || rezPlatforms.error || null,
      },
    });
    setDetailsVisible(true);
    selectDetail("adapters", false);
    const readReady = adapterMatrix.filter((row) => row.read_supported).length;
    const writeReady = adapterMatrix.filter((row) => row.write_supported).length;
    completeAction("show-adapter-matrix", {
      status: rezHealth.ok ? "pass" : "info",
      actual: `${readReady} read platforms from Rez. ${writeReady} write adapter currently enabled.`,
      evidence: rezHealth.ok
        ? `Rez root: ${rezHealth.root}. Adapter matrix visible in Technical details > Adapters.`
        : `Rez unavailable: ${rezHealth.error || "no driver registry loaded"}. Planned write adapters remain locked.`,
    });
  } catch (error) {
    failAction("show-adapter-matrix", error.message);
  }
}

function populateDiscoveryPlatforms(platforms = []) {
  const select = $("discovery-platform");
  if (!select) return;
  const current = select.value;
  const labels = {
    arista_eos: "Arista EOS",
    cisco_ios: "Cisco IOS/XE",
    cisco_nxos: "Cisco NX-OS",
    cisco_asa: "Cisco ASA",
    juniper_junos: "Juniper Junos",
    aruba_aoscx: "Aruba AOS-CX",
    nokia_srl: "Nokia SR Linux",
    fortinet: "Fortinet",
    palo_alto: "Palo Alto",
    meraki: "Meraki",
    cisco_sdwan: "Cisco SD-WAN",
  };
  platforms.forEach((platform) => {
    if (!platform || select.querySelector(`option[value="${platform}"]`)) return;
    const option = document.createElement("option");
    option.value = platform;
    option.textContent = labels[platform] || platform.replaceAll("_", " ");
    select.appendChild(option);
  });
  select.value = current;
}

function discoveryPayload() {
  const form = formPayload();
  return {
    host: $("discovery-host").value.trim(),
    platform: $("discovery-platform").value,
    username: $("discovery-username").value.trim(),
    password: $("discovery-password").value,
    device_id: form.device_id || "",
    site: form.site || "",
    groups: [],
    port: 22,
  };
}

function discoveryCommandSummary(data) {
  const adapter = data.adapter || (data.platform ? `rez.${data.platform}` : "Rez driver");
  return [
    `# Discovery uses read/state collection only`,
    `$ ${adapter}: connect`,
    `$ ${adapter}: collect device state`,
    `$ ${adapter}: disconnect`,
    `# Device writes: none`,
  ].join("\n");
}

async function runDiscovery() {
  startAction("run-discovery");
  setBusy(true);
  try {
    const data = await postJson("/api/discovery/scan", discoveryPayload());
    $("detail-discovery").textContent = formatJson(data);
    setDetailsVisible(true);
    selectDetail("discovery", false);
    if (data.ok && data.source_of_truth_candidate) {
      lastDiscoveryCandidate = data.source_of_truth_candidate;
      const summary = data.state_summary || {};
      completeAction("run-discovery", {
        status: "pass",
        actual: `Discovered ${data.platform} device ${summary.hostname || data.host} at ${data.host}.`,
        evidence: `Rez adapter ${data.adapter || data.platform} returned state. Source-of-truth candidate is ready to review.`,
        commands: discoveryCommandSummary(data),
      });
    } else {
      lastDiscoveryCandidate = null;
      completeAction("run-discovery", {
        status: "fail",
        actual: data.error || "Rez could not discover this device with the tried platform drivers.",
        evidence: `${(data.tried_platforms || []).length} platform attempt(s). No source-of-truth record was written.`,
        commands: discoveryCommandSummary(data),
      });
    }
    renderDiscoveryImport();
  } catch (error) {
    lastDiscoveryCandidate = null;
    renderDiscoveryImport();
    failAction("run-discovery", error.message);
  } finally {
    setBusy(false);
  }
}

async function importDiscoveredDevice() {
  startAction("import-discovered-device");
  if (!lastDiscoveryCandidate) {
    failAction("import-discovered-device", "Run discovery first so there is a reviewed source-of-truth candidate to save.");
    return;
  }
  setBusy(true);
  try {
    const data = await postJson("/api/source-of-truth/devices/import", { candidate: lastDiscoveryCandidate });
    const source = await getJson("/api/source-of-truth");
    $("detail-discovery").textContent = formatJson({ import: data, candidate: lastDiscoveryCandidate });
    $("detail-source").textContent = formatJson(source);
    setDetailsVisible(true);
    selectDetail("source", false);
    completeAction("import-discovered-device", {
      status: data.ok ? "pass" : "fail",
      actual: data.message || "Source-of-truth import completed.",
      evidence: `Inventory file: ${data.inventory || "not written"}. Device count: ${source.summary?.device_count || "unknown"}.`,
      commands: `# Local file update only\n$ update ${data.inventory || "inventories/lab.yaml"}\n# Discovery password was not written to source of truth.`,
    });
    await refreshPlatform({ log: false });
  } catch (error) {
    failAction("import-discovered-device", error.message);
  } finally {
    setBusy(false);
  }
}

async function showWorkflowRules() {
  startAction("show-workflow-rules");
  try {
    const snapshots = await Promise.all(WORKFLOW_STATES.map((state) => getJson(`/api/workflow/state/${state}`)));
    const rules = Object.fromEntries(snapshots.map((snapshot) => [snapshot.state, snapshot]));
    $("detail-workflow").textContent = formatJson(rules);
    setDetailsVisible(true);
    selectDetail("workflow", false);
    completeAction("show-workflow-rules", {
      status: "pass",
      actual: `${snapshots.length} workflow states loaded with allowed and blocked actions.`,
      evidence: "State-machine contract is visible in Technical details > Workflow.",
    });
  } catch (error) {
    failAction("show-workflow-rules", error.message);
  }
}

function requireCurrentIntent(actionKey) {
  if (currentIntentPath) return true;
  failAction(actionKey, "Run safety checks first so the platform has an intent artifact to analyze.");
  return false;
}

async function showGitOpsPlan() {
  startAction("show-gitops-plan");
  if (!requireCurrentIntent("show-gitops-plan")) return;
  try {
    const data = await postJson("/api/gitops/plan", { intent_path: currentIntentPath, device_id: formPayload().device_id, change_id: currentChangeId || null });
    $("detail-gitops").textContent = formatJson(data);
    setDetailsVisible(true);
    selectDetail("gitops", false);
    completeAction("show-gitops-plan", {
      status: "pass",
      actual: `GitOps plan created for branch ${data.suggested_branch}.`,
      evidence: `${data.artifacts?.length || 0} artifacts listed. Git available: ${Boolean(data.git_available)}.`,
      commands: formatCommandList(data.evidence?.suggested_commands || []),
    });
  } catch (error) {
    failAction("show-gitops-plan", error.message);
  }
}

async function showConformance() {
  startAction("show-conformance");
  try {
    const data = await getJson("/api/adapters/conformance");
    $("detail-adapters").textContent = formatJson(data);
    setDetailsVisible(true);
    selectDetail("adapters", false);
    const rows = data.conformance || [];
    const pass = rows.filter((row) => row.status === "pass").length;
    const partial = rows.filter((row) => row.status === "partial").length;
    completeAction("show-conformance", {
      status: "pass",
      actual: `${pass} full adapter contract, ${partial} partial contracts, ${rows.length} total platforms.`,
      evidence: "Conformance is visible in Technical details > Adapters.",
    });
  } catch (error) {
    failAction("show-conformance", error.message);
  }
}

async function showVerificationCatalog() {
  startAction("show-verification-catalog");
  const catalog = {
    checks: [
      "vlan_exists",
      "vlan_absent",
      "interface_state",
      "bgp_neighbor_established",
      "route_present",
      "prefix_not_leaking",
      "management_reachable",
    ],
    read_provider: "Rez live state",
    behavior: "pass, fail, or unsupported with evidence; never crashes the workflow for missing vendor support.",
  };
  $("detail-drift").textContent = formatJson(catalog);
  setDetailsVisible(true);
  selectDetail("drift", false);
  completeAction("show-verification-catalog", {
    status: "pass",
    actual: `${catalog.checks.length} verification contracts are available.`,
    evidence: "Catalog is visible in Technical details > Drift.",
  });
}

async function runDriftCheck() {
  startAction("run-drift-check");
  if (!requireCurrentIntent("run-drift-check")) return;
  try {
    const data = await postJson("/api/drift/vlan", { intent_path: currentIntentPath, device_id: formPayload().device_id, change_id: currentChangeId || null });
    $("detail-drift").textContent = formatJson(data);
    setDetailsVisible(true);
    selectDetail("drift", false);
    completeAction("run-drift-check", {
      status: data.status === "in_sync" ? "pass" : data.status === "unknown" ? "info" : "fail",
      actual: data.message,
      evidence: `Drift status: ${data.status}. Severity: ${data.severity}.`,
    });
  } catch (error) {
    failAction("run-drift-check", error.message);
  }
}

async function showScalePlan() {
  startAction("show-scale-plan");
  try {
    const data = await postJson("/api/scale/plan", { device_ids: null, canary_size: 1, batch_size: 100 });
    $("detail-scale").textContent = formatJson(data);
    setDetailsVisible(true);
    selectDetail("scale", false);
    completeAction("show-scale-plan", {
      status: "pass",
      actual: `${data.device_count} devices planned with ${data.canary_size} canary device(s).`,
      evidence: "Canary, batch, lock, retry, and pause controls are visible in Technical details > Scale.",
    });
  } catch (error) {
    failAction("show-scale-plan", error.message);
  }
}

async function askAssistant() {
  startAction("ask-assistant");
  try {
    let workflowContext = {};
    try {
      workflowContext = JSON.parse($("detail-workflow").textContent || "{}");
    } catch {
      workflowContext = {};
    }
    const data = await postJson("/api/assistant", {
      prompt: $("assistant-prompt").value,
      context: {
        workflow: workflowContext,
        form: formPayload(),
        current_intent_path: currentIntentPath,
        current_change_id: currentChangeId,
      },
    });
    $("detail-assistant").textContent = formatJson(data);
    setDetailsVisible(true);
    selectDetail("assistant", false);
    completeAction("ask-assistant", {
      status: "pass",
      actual: data.answer,
      evidence: `Assistant mode: ${data.mode}. Guardrails: ${(data.guardrails || []).length}.`,
    });
  } catch (error) {
    failAction("ask-assistant", error.message);
  }
}

async function showJobs() {
  startAction("show-jobs");
  try {
    const data = await getJson("/api/jobs");
    $("detail-jobs").textContent = formatJson(data);
    setDetailsVisible(true);
    selectDetail("jobs", false);
    completeAction("show-jobs", {
      status: "pass",
      actual: `${data.jobs?.length || 0} job records loaded.`,
      evidence: "Job records are visible in Technical details > Jobs.",
    });
  } catch (error) {
    failAction("show-jobs", error.message);
  }
}

async function showDeviceState() {
  startAction("show-device-state");
  try {
    const data = await postJson("/api/adapters/rez/collect-state", { device_id: formPayload().device_id });
    $("detail-lab").textContent = formatJson(data);
    setDetailsVisible(true);
    selectDetail("lab", false);
    completeAction("show-device-state", {
      status: data.ok === false ? "fail" : "pass",
      actual: data.ok === false ? "State adapter returned unavailable or unsupported." : "Device state response returned.",
      evidence: "Device state response is visible in Technical details > Device Proof.",
    });
  } catch (error) {
    failAction("show-device-state", error.message);
  }
}

async function verifyVlanState() {
  startAction("verify-vlan-state");
  try {
    const payload = formPayload();
    const data = await postJson("/api/verify/vlan", {
      device_id: payload.device_id,
      vlan_id: payload.vlan_id,
      name: payload.name,
      present: true,
    });
    const verification = data.verification || {};
    $("detail-lab").textContent = formatJson(data);
    setDetailsVisible(true);
    selectDetail("lab", false);
    completeAction("verify-vlan-state", {
      status: verification.status === "pass" ? "pass" : verification.status === "unsupported" ? "info" : "fail",
      actual: verification.message || "VLAN verification completed.",
      evidence: `State adapter: ${data.state?.adapter || "unknown"}. Collection: ${data.state?.collection_time ?? "n/a"}s.`,
    });
  } catch (error) {
    failAction("verify-vlan-state", error.message);
  }
}

function journeyStepById(id) {
  return JOURNEY_STEPS.find((step) => step.id === id) || JOURNEY_STEPS[0];
}

function journeyActionKey(stepId) {
  return {
    intent: "check-safety",
    dryrun: "test-candidate",
    apply: "apply-change",
    rollback: "rollback",
  }[stepId];
}

function setJourneyStatus(status, label) {
  const pill = $("journey-step-status");
  pill.className = "journey-status-pill";
  pill.classList.add(`state-${status}`);
  pill.textContent = label;
}

function renderJourneyProgress() {
  const total = JOURNEY_STEPS.length;
  $("journey-progress").textContent = `${journeyCompletedSteps.size}/${total} complete`;
  const latest = JOURNEY_STEPS.filter((step) => journeyCompletedSteps.has(step.id)).at(-1);
  $("journey-current-artifact").textContent = latest
    ? `Latest artifact: ${journeyArtifacts[latest.id]?.artifact || latest.title}`
    : "Define the change first";
}

function defaultJourneyPlain(step) {
  const currentIndex = JOURNEY_STEP_IDS.indexOf(step.id);
  const missing = currentIndex > 0 ? JOURNEY_STEPS.slice(0, currentIndex).find((candidate) => !journeyCompletedSteps.has(candidate.id)) : null;
  return {
    title: missing ? "What needs to happen first?" : "What should I learn in this step?",
    summary: missing
      ? `Complete Step ${missing.number} ${missing.title} first. This journey is ordered so every step has real evidence from the previous step.`
      : step.copy,
    checks: [
      {
        title: missing ? "Required previous step" : "Your action",
        detail: missing ? `Run Step ${missing.number}: ${missing.title}.` : `Click "${step.button}" to run or inspect the real platform artifact for this step.`,
        state: missing ? "warn" : "info",
      },
      {
        title: "Next decision",
        detail: missing ? "After the previous step completes, come back and run this step." : step.next,
        state: "info",
      },
    ],
    decision: missing ? "Decision: waiting for the earlier artifact." : "Run the step to see the platform decision in plain English.",
  };
}

function renderJourneyPlain(step, stored) {
  const plain = stored?.plain || defaultJourneyPlain(step);
  $("journey-plain-title").textContent = plain.title;
  $("journey-plain-summary").textContent = plain.summary;
  $("journey-decision").textContent = plain.decision;

  const checks = $("journey-plain-checks");
  checks.innerHTML = "";
  plain.checks.forEach((item) => {
    const card = document.createElement("article");
    card.className = item.state || "info";
    const title = document.createElement("strong");
    title.textContent = item.title;
    const detail = document.createElement("span");
    detail.textContent = item.detail;
    card.appendChild(title);
    card.appendChild(detail);
    checks.appendChild(card);
  });
}

function renderJourneyStep() {
  if (!$("journey-step-title")) return;
  const step = journeyStepById(journeyCurrentStep);
  const stored = journeyArtifacts[step.id];
  $("journey-step-eyebrow").textContent = `Step ${step.number}`;
  $("journey-step-title").textContent = step.title;
  $("journey-step-copy").textContent = step.copy;
  $("journey-run-step").textContent = step.button;
  $("journey-creates").textContent = step.creates;
  $("journey-artifact").textContent = stored?.artifact || "Waiting for step output.";
  $("journey-why").textContent = step.why;
  $("journey-next").textContent = step.next;
  $("journey-output").textContent = stored?.output || "Click the step action to inspect or create the real artifact for this step.";
  renderJourneyPlain(step, stored);

  if (journeyFailedSteps.has(step.id)) {
    setJourneyStatus("fail", stored?.statusLabel || "Blocked");
  } else if (journeyCompletedSteps.has(step.id)) {
    setJourneyStatus("pass", "Completed");
  } else {
    setJourneyStatus("ready", "Ready");
  }

  document.querySelectorAll(".journey-step").forEach((button) => {
    const id = button.dataset.journeyStep;
    button.classList.toggle("active", id === step.id);
    button.classList.toggle("complete", journeyCompletedSteps.has(id));
    button.classList.toggle("failed", journeyFailedSteps.has(id));
  });
  renderJourneyProgress();
}

function selectJourneyStep(stepId) {
  journeyCurrentStep = stepId;
  renderJourneyStep();
}

function switchMode(mode) {
  const consoleActive = mode === "console";
  $("console-view").classList.toggle("hidden", !consoleActive);
  $("journey-view").classList.toggle("hidden", consoleActive);
  $("mode-console").classList.toggle("active", consoleActive);
  $("mode-journey").classList.toggle("active", !consoleActive);
  if (!consoleActive) renderJourneyStep();
}

function appendJourneyEntry(step, status, artifact, summary) {
  const journal = $("journey-journal");
  const empty = journal.querySelector(".journey-empty");
  if (empty) empty.remove();

  const entry = document.createElement("article");
  entry.className = `journey-entry ${status}`;
  const header = document.createElement("header");
  const title = document.createElement("strong");
  title.textContent = `${step.number} ${step.title}`;
  const time = document.createElement("span");
  time.textContent = timestamp();
  header.appendChild(title);
  header.appendChild(time);

  const artifactLine = document.createElement("p");
  artifactLine.textContent = `Artifact: ${artifact}`;
  const summaryLine = document.createElement("p");
  summaryLine.textContent = `Outcome: ${summary}`;
  const nextLine = document.createElement("p");
  nextLine.textContent = `Next: ${step.next}`;

  entry.appendChild(header);
  entry.appendChild(artifactLine);
  entry.appendChild(summaryLine);
  entry.appendChild(nextLine);
  journal.prepend(entry);

  while (journal.querySelectorAll(".journey-entry").length > 12) {
    journal.querySelector(".journey-entry:last-child").remove();
  }
}

function completeJourneyStep(step, { artifact, output, summary, plain, status = "pass", statusLabel = null }) {
  journeyArtifacts[step.id] = { artifact, output, summary, plain, statusLabel };
  if (status === "pass") {
    journeyCompletedSteps.add(step.id);
    journeyFailedSteps.delete(step.id);
  } else {
    journeyFailedSteps.add(step.id);
    journeyCompletedSteps.delete(step.id);
  }
  $("journey-artifact").textContent = artifact;
  $("journey-output").textContent = output;
  renderJourneyPlain(step, journeyArtifacts[step.id]);
  appendJourneyEntry(step, status, artifact, summary);
  setJourneyStatus(status, status === "pass" ? "Completed" : statusLabel || "Blocked");
  renderJourneyProgress();
}

function requireJourneyIntent() {
  if (currentIntentPath && journeyPipelineData) return;
  throw new Error("Run Step 02 Intent YAML first. The platform needs a real request artifact before this step can inspect downstream evidence.");
}

function requireJourneyDryRun() {
  requireJourneyIntent();
  if (safetyPassed) return;
  throw new Error("Static validation has not passed. Run Step 06 or recreate the intent before lab testing.");
}

function requireJourneyApply() {
  requireJourneyDryRun();
  if (dryRunPassed) return;
  throw new Error("Run Step 07 Lab Dry-Run first. Apply stays locked until EOS accepts the candidate in an aborted config session.");
}

function requirePreviousJourneySteps(stepId) {
  const index = JOURNEY_STEP_IDS.indexOf(stepId);
  if (index <= 0) return;
  const missing = JOURNEY_STEPS.slice(0, index).find((step) => !journeyCompletedSteps.has(step.id));
  if (!missing) return;
  const current = journeyStepById(stepId);
  if (stepId === "gitops" && missing.id === "intent") {
    throw new Error("Create the intent YAML first. Git needs a real file to add, branch, commit, and review. Click Step 02, run Create intent YAML, then return to Git Setup.");
  }
  throw new Error(`Complete Step ${missing.number} ${missing.title} first. ${current.title} depends on evidence from that earlier step.`);
}

function formatValidationChecks(checks) {
  return checks.map((check) => `${check.status.toUpperCase()} ${check.id}: ${check.message}`).join("\n");
}

function validateChangeRequest(payload) {
  const errors = [];
  if (!payload.site) errors.push("Site is required.");
  if (!payload.device_id) errors.push("Device is required.");
  if (!Number.isInteger(payload.vlan_id) || payload.vlan_id < 2 || payload.vlan_id > 4094) errors.push("VLAN ID must be between 2 and 4094.");
  if (!payload.name) errors.push("VLAN name is required.");
  if (!payload.subnet || !payload.subnet.includes("/")) errors.push("Subnet must be CIDR notation, for example 10.42.90.0/24.");
  if (!payload.purpose) errors.push("Purpose is required.");
  if (!payload.requested_by) errors.push("Requested By is required.");
  return errors;
}

async function executeJourneyStep(stepId) {
  if (stepId === "define") {
    const payload = formPayload();
    const errors = validateChangeRequest(payload);
    const ok = errors.length === 0;
    if (ok) journeyDefinedRequest = payload;
    const requestSummary = `Add VLAN ${payload.vlan_id || "?"} (${payload.name || "unnamed"}) to ${payload.device_id || "no device"} at ${payload.site || "no site"}`;
    return {
      artifact: ok ? `Request defined: ${requestSummary}` : "Request is incomplete",
      summary: ok ? "The requested network change is defined. No files were created and no device was contacted." : errors.join(" "),
      output: formatJson({
        change_type: "add_vlan",
        status: ok ? "defined" : "incomplete",
        request: payload,
        errors,
      }),
      status: ok ? "pass" : "fail",
      statusLabel: ok ? null : "Needs input",
      plain: {
        title: ok ? "What change did you ask the platform to make?" : "What is missing from the request?",
        summary: ok
          ? "You confirmed the change request. The platform can now check trusted data before creating YAML, config, or lab evidence."
          : "The platform needs a complete request before it can safely inspect, build, or test anything.",
        checks: ok
          ? [
              {
                title: "Change type",
                detail: "Add VLAN",
                state: "pass",
              },
              {
                title: "Target",
                detail: `${payload.device_id} at ${payload.site}`,
                state: "pass",
              },
              {
                title: "VLAN",
                detail: `VLAN ${payload.vlan_id}, name ${payload.name}, subnet ${payload.subnet}, purpose ${payload.purpose}`,
                state: "pass",
              },
              {
                title: "Device touched",
                detail: "No. This only confirms what you want to do.",
                state: "pass",
              },
            ]
          : errors.map((error) => ({
              title: "Missing input",
              detail: error,
              state: "fail",
            })),
        decision: ok ? "Decision: request is defined. Next, check source of truth for this target." : "Decision: complete the request fields before continuing.",
      },
    };
  }

  if (stepId === "source") {
    const data = await getJson("/api/source-of-truth");
    const payload = journeyDefinedRequest || formPayload();
    const target = (data.devices || []).find((device) => device.id === payload.device_id);
    const policyGroups = Object.keys(data.policies || {});
    const templates = data.templates || [];
    const knownSubnets = data.known_subnets?.[payload.site] || [];
    $("detail-source").textContent = formatJson(data);
    return {
      artifact: target
        ? `Ready: ${payload.device_id} found, ${templates.length} template, ${policyGroups.length} policy groups`
        : `Blocked: ${payload.device_id} is not in source of truth`,
      summary: target ? "The target device, policy groups, and template catalog are known." : "The selected device is missing from source of truth.",
      output: formatJson(data),
      status: target ? "pass" : "fail",
      plain: {
        title: "Can the platform safely start this change?",
        summary: target
          ? `Yes. The platform knows ${payload.device_id}, its management IP, its vendor type, the policy rules, and the template catalog.`
          : `No. ${payload.device_id} was not found in the trusted inventory, so the platform should not build a change for it.`,
        checks: [
          {
            title: target ? "Target switch found" : "Target switch missing",
            detail: target ? `${target.id} is ${target.platform} at ${target.host}:${target.port}.` : `${payload.device_id} is not in inventories/lab.yaml.`,
            state: target ? "pass" : "fail",
          },
          {
            title: "Policy groups loaded",
            detail: policyGroups.length ? policyGroups.join(", ") : "No policy groups were loaded.",
            state: policyGroups.length ? "pass" : "fail",
          },
          {
            title: "Template catalog loaded",
            detail: templates.length ? templates.join(", ") : "No templates were found.",
            state: templates.length ? "pass" : "fail",
          },
          {
            title: "Known site subnets",
            detail: knownSubnets.length ? knownSubnets.join(", ") : `No known subnets for ${payload.site}.`,
            state: knownSubnets.length ? "pass" : "warn",
          },
        ],
        decision: target ? "Decision: safe to continue. Next, create the intent YAML." : "Decision: blocked. Fix inventory before continuing.",
      },
    };
  }

  if (stepId === "intent") {
    startAction("check-safety");
    resetProofGates();
    setAssurance("policy", "running", "Checking", "Building candidate and running policy validation.");
    const payload = journeyDefinedRequest || formPayload();
    const data = await postJson("/api/wizard/add-vlan", payload);
    journeyPipelineData = data;
    updateFromPipeline(data);
    return {
      artifact: data.intent_path,
      summary: data.ok ? "Intent YAML created and static validation passed." : "Intent YAML created but validation blocked the request.",
      output: data.pipeline.intent_yaml,
      status: data.ok ? "pass" : "fail",
      plain: {
        title: "What request file did the platform create?",
        summary: `It wrote a YAML request for VLAN ${payload.vlan_id} named ${payload.name} on ${payload.device_id}. No device was contacted in this step.`,
        checks: [
          {
            title: "Change request captured",
            detail: `Add VLAN ${payload.vlan_id} to ${payload.device_id} at ${payload.site}.`,
            state: "pass",
          },
          {
            title: "Intent file created",
            detail: data.intent_path,
            state: "pass",
          },
          {
            title: "Device touched",
            detail: "No. This step only wrote files and ran static checks.",
            state: "pass",
          },
          {
            title: data.ok ? "Safety gate" : "Safety gate blocked",
            detail: data.ok ? "Static checks passed, so lab dry-run can be unlocked later." : "Static checks failed. Review validation before continuing.",
            state: data.ok ? "pass" : "fail",
          },
        ],
        decision: data.ok ? "Decision: intent is ready. Next, check Git setup and review flow." : "Decision: blocked. Fix the request before continuing.",
      },
    };
  }

  if (stepId === "gitops") {
    requireJourneyIntent();
    const data = await postJson("/api/gitops/plan", {
      intent_path: currentIntentPath,
      device_id: (journeyDefinedRequest || formPayload()).device_id,
      change_id: currentChangeId || null,
    });
    $("detail-gitops").textContent = formatJson(data);
    const setup = data.repository_setup || {};
    const setupCommands = setup.commands || (data.git_available ? ["git status"] : ["git init", "git status"]);
    const reviewCommands = data.evidence?.suggested_commands || [];
    const allCommands = [...setupCommands, ...reviewCommands];
    return {
      artifact: data.git_available ? `Repo ready: branch ${data.branch || data.suggested_branch}` : `Git repo not initialized in ${data.workspace}`,
      summary: data.git_available ? "Git repo is ready for branch, add, commit, and PR review." : "Git repo setup is needed before this becomes a reviewable change workflow.",
      output: `${formatCommandList(allCommands, "No suggested git commands were returned.")}\n\n${formatJson(data)}`,
      plain: {
        title: data.git_available ? "Is this platform folder already tracked in Git?" : "How does a network engineer create the Git repo?",
        summary: data.git_available
          ? `Yes. The platform folder is already a Git repo. The intent can be reviewed on branch ${data.suggested_branch}.`
          : `Not yet. To make network changes reviewable, initialize Git once in ${data.workspace}, then add and commit the intent artifacts.`,
        checks: [
          {
            title: data.git_available ? "Git repository found" : "Git repository missing",
            detail: setup.message || (data.git_available ? "This folder has a .git directory." : "Run git init once in the platform folder."),
            state: data.git_available ? "pass" : "warn",
          },
          {
            title: "Setup command",
            detail: setupCommands.join(" && "),
            state: data.git_available ? "pass" : "warn",
          },
          {
            title: "Change branch",
            detail: `Use ${data.suggested_branch} so this network change has its own review path.`,
            state: "pass",
          },
          {
            title: "Review commands",
            detail: reviewCommands.join(" && "),
            state: "pass",
          },
        ],
        decision: data.git_available
          ? "Decision: Git is ready. Next, inspect the template that will generate device config."
          : "Decision: Git setup required for production workflow. For this lab journey, continue to inspect the template.",
      },
    };
  }

  if (stepId === "template") {
    const data = await getJson("/api/templates/arista/add_vlan");
    return {
      artifact: data.path,
      summary: "Jinja template inspected from the platform template directory.",
      output: `# ${data.path}\n${data.body}`,
      plain: {
        title: "How does the platform turn intent into config?",
        summary: "It uses a Jinja template. For this VLAN workflow, the template creates a VLAN and name, and only creates an SVI if the request asks for one.",
        checks: [
          {
            title: "Template file",
            detail: data.path,
            state: "pass",
          },
          {
            title: "Vendor workflow",
            detail: "Arista add-VLAN template.",
            state: "pass",
          },
          {
            title: "Required values",
            detail: "vlan.id and vlan.name are inserted from the intent YAML.",
            state: "pass",
          },
          {
            title: "Conditional config",
            detail: "SVI commands render only when vlan.svi.enabled is true.",
            state: "pass",
          },
        ],
        decision: "Decision: template is ready. Next, inspect the rendered candidate config.",
      },
    };
  }

  if (stepId === "candidate") {
    requireJourneyIntent();
    const pipeline = journeyPipelineData.pipeline;
    const commands = commandListFromText(pipeline.render.config);
    $("detail-config").textContent = pipeline.render.config;
    return {
      artifact: pipeline.artifacts?.rendered_path || pipeline.render.template_path,
      summary: "Rendered EOS candidate config inspected before device contact.",
      output: `# Candidate config\n${pipeline.render.config}\n# Template variables\n${formatJson(pipeline.render.variables)}`,
      plain: {
        title: "What exact config would be sent to the switch?",
        summary: `The platform generated ${commands.length} EOS command lines from the YAML intent and Jinja template. No device has received these commands yet.`,
        checks: [
          {
            title: "Candidate commands",
            detail: commands.join(" | "),
            state: "pass",
          },
          {
            title: "Rendered artifact",
            detail: pipeline.artifacts?.rendered_path || "Rendered config path not reported.",
            state: "pass",
          },
          {
            title: "Device touched",
            detail: "No. This is still file generation and review.",
            state: "pass",
          },
          {
            title: "Template variables",
            detail: `VLAN ${pipeline.render.variables?.vlan?.id} named ${pipeline.render.variables?.vlan?.name}.`,
            state: "pass",
          },
        ],
        decision: "Decision: candidate config is visible. Next, inspect policy validation.",
      },
    };
  }

  if (stepId === "validation") {
    requireJourneyIntent();
    const pipeline = journeyPipelineData.pipeline;
    updateChecks(pipeline.validation.checks);
    const failedChecks = pipeline.validation.checks.filter((check) => check.status !== "pass");
    return {
      artifact: pipeline.artifacts?.report_markdown_path || "Static validation report",
      summary: `${pipeline.validation.checks.length}/${pipeline.validation.checks.length} policy checks inspected. Status: ${pipeline.validation.status}.`,
      output: `${formatValidationChecks(pipeline.validation.checks)}\n\n${formatJson(pipeline.validation)}`,
      status: pipeline.validation.status === "pass" ? "pass" : "fail",
      plain: {
        title: "Did policy allow this request to reach the lab?",
        summary:
          pipeline.validation.status === "pass"
            ? `Yes. ${pipeline.validation.checks.length} static checks passed before any device was contacted.`
            : `No. ${failedChecks.length} checks failed, so the platform must stop before lab testing.`,
        checks: pipeline.validation.checks.slice(0, 6).map((check) => ({
          title: `${check.status.toUpperCase()}: ${check.title}`,
          detail: check.message,
          state: check.status === "pass" ? "pass" : "fail",
        })),
        decision:
          pipeline.validation.status === "pass"
            ? "Decision: safe to test in the lab. Next, run dry-run without committing."
            : "Decision: blocked. Fix the request or policy violation before continuing.",
      },
    };
  }

  if (stepId === "dryrun") {
    requireJourneyDryRun();
    startAction("test-candidate");
    setAssurance("lab", "running", "Dry-run running", "Waiting for EOS proof.");
    setAssurance("commands", "running", "Sending commands", "Command transcript will appear when the device responds.");
    const data = await postJson("/api/lab/dry-run", {
      intent_path: currentIntentPath,
      device_id: (journeyDefinedRequest || formPayload()).device_id,
      change_id: currentChangeId || null,
    });
    updateFromLabResult(data);
    return {
      artifact: data.job ? `Job ${data.job.id}` : `Config session ${data.result?.session_name || "not reported"}`,
      summary: data.result?.message || "Dry-run completed.",
      output: `${commandTranscriptText(data)}\n\n${formatJson(data)}`,
      status: data.ok ? "pass" : "fail",
      plain: {
        title: data.ok ? "Did the lab switch accept the config safely?" : "Did the lab switch reject the config?",
        summary: data.ok
          ? "Yes. EOS accepted the candidate in a config session, the platform captured the diff, and then aborted the session. Nothing was committed."
          : "No. The lab dry-run failed, so apply remains locked.",
        checks: [
          {
            title: "Config session",
            detail: data.result?.session_name || "Session name not reported.",
            state: data.ok ? "pass" : "fail",
          },
          {
            title: "Commands sent",
            detail: formatCommandList(commandListFromLabResult(data.result || data), "No commands reported."),
            state: data.ok ? "pass" : "fail",
          },
          {
            title: "Commit status",
            detail: "No commit. Dry-run ends with abort.",
            state: data.ok ? "pass" : "warn",
          },
          {
            title: "Apply lock",
            detail: data.ok ? "Unlocked because dry-run proof passed." : "Still locked because dry-run proof failed.",
            state: data.ok ? "pass" : "fail",
          },
        ],
        decision: data.ok ? "Decision: lab proof passed. Next, apply and verify in the lab." : "Decision: blocked. Fix candidate config before apply.",
      },
    };
  }

  if (stepId === "apply") {
    requireJourneyApply();
    startAction("apply-change");
    setAssurance("lab", "running", "Applying", "Waiting for EOS proof.");
    setAssurance("commands", "running", "Sending commands", "Command transcript will appear when the device responds.");
    const data = await postJson("/api/lab/apply", {
      intent_path: currentIntentPath,
      device_id: (journeyDefinedRequest || formPayload()).device_id,
      change_id: currentChangeId || null,
    });
    updateFromLabResult(data);
    return {
      artifact: data.job ? `Job ${data.job.id}` : `Config session ${data.result?.session_name || "not reported"}`,
      summary: data.result?.message || "Apply completed.",
      output: `${commandTranscriptText(data)}\n\n${formatJson(data)}`,
      status: data.ok ? "pass" : "fail",
      plain: {
        title: data.ok ? "Was the lab change applied and verified?" : "Did apply fail?",
        summary: data.ok
          ? "Yes. The platform committed the tested candidate and verified VLAN 90 exists on the lab switch."
          : "No. The platform did not complete apply safely.",
        checks: [
          {
            title: "Commit session",
            detail: data.result?.session_name || "Session name not reported.",
            state: data.ok ? "pass" : "fail",
          },
          {
            title: "Commands committed",
            detail: formatCommandList(commandListFromLabResult(data.result || data), "No commands reported."),
            state: data.ok ? "pass" : "fail",
          },
          {
            title: "Post-check",
            detail: data.result?.message || "Verification result not reported.",
            state: data.ok ? "pass" : "fail",
          },
          {
            title: "Rollback lock",
            detail: data.ok ? "Unlocked because the lab change now exists." : "Not unlocked because apply failed.",
            state: data.ok ? "pass" : "fail",
          },
        ],
        decision: data.ok ? "Decision: lab apply verified. Next, prove rollback." : "Decision: stop and review evidence before retry.",
      },
    };
  }

  if (stepId === "rollback") {
    requireJourneyApply();
    if (!applyPassed) throw new Error("Run Step 08 Apply + Verify first. Rollback is locked until there is a lab change to remove.");
    startAction("rollback");
    setAssurance("lab", "running", "Rolling back", "Waiting for EOS proof.");
    setAssurance("commands", "running", "Sending commands", "Command transcript will appear when the device responds.");
    const data = await postJson("/api/lab/rollback", {
      intent_path: currentIntentPath,
      device_id: (journeyDefinedRequest || formPayload()).device_id,
      change_id: currentChangeId || null,
    });
    updateFromLabResult(data);
    return {
      artifact: data.job ? `Job ${data.job.id}` : `Config session ${data.result?.session_name || "not reported"}`,
      summary: data.result?.message || "Rollback completed.",
      output: `${commandTranscriptText(data)}\n\n${formatJson(data)}`,
      status: data.ok ? "pass" : "fail",
      plain: {
        title: data.ok ? "Did rollback return the lab to the expected state?" : "Did rollback fail?",
        summary: data.ok
          ? "Yes. The platform removed the VLAN and verified it is absent from the lab switch."
          : "No. Rollback did not complete safely, so the evidence must be reviewed.",
        checks: [
          {
            title: "Rollback session",
            detail: data.result?.session_name || "Session name not reported.",
            state: data.ok ? "pass" : "fail",
          },
          {
            title: "Commands committed",
            detail: formatCommandList(commandListFromLabResult(data.result || data), "No commands reported."),
            state: data.ok ? "pass" : "fail",
          },
          {
            title: "Post-check",
            detail: data.result?.message || "Verification result not reported.",
            state: data.ok ? "pass" : "fail",
          },
          {
            title: "Lab state",
            detail: data.ok ? "Clean for the next run." : "Needs operator review.",
            state: data.ok ? "pass" : "fail",
          },
        ],
        decision: data.ok ? "Decision: rollback verified. Next, inspect the evidence package." : "Decision: stop and review rollback evidence.",
      },
    };
  }

  if (stepId === "evidence") {
    requireJourneyIntent();
    const [workflow, jobs, gitops] = await Promise.all([
      currentChangeId ? getJson(`/api/workflow/change/${currentChangeId}`) : Promise.resolve({ change: null, events: [] }),
      getJson("/api/jobs"),
      postJson("/api/gitops/plan", { intent_path: currentIntentPath, device_id: (journeyDefinedRequest || formPayload()).device_id, change_id: currentChangeId || null }),
    ]);
    const evidence = {
      change_id: currentChangeId,
      intent_path: currentIntentPath,
      workflow,
      latest_jobs: jobs.jobs?.slice(0, 8) || [],
      gitops,
      reports: journeyPipelineData?.pipeline?.artifacts || {},
    };
    $("detail-workflow").textContent = formatJson(workflow);
    $("detail-jobs").textContent = formatJson(jobs);
    return {
      artifact: currentChangeId || currentIntentPath,
      summary: `${workflow.events?.length || 0} workflow events and ${jobs.jobs?.length || 0} jobs inspected.`,
      output: formatJson(evidence),
      plain: {
        title: "What proof can the engineer show after the change?",
        summary: "The platform collected the request, validation result, Git review plan, job history, workflow events, command transcripts, and report paths.",
        checks: [
          {
            title: "Change record",
            detail: currentChangeId || "Change ID not reported.",
            state: currentChangeId ? "pass" : "warn",
          },
          {
            title: "Workflow state",
            detail: workflow.workflow?.state || workflow.change?.workflow_state || "Workflow state not reported.",
            state: "pass",
          },
          {
            title: "Job records",
            detail: `${jobs.jobs?.length || 0} total jobs available in the platform store.`,
            state: jobs.jobs?.length ? "pass" : "warn",
          },
          {
            title: "Reports",
            detail: Object.values(journeyPipelineData?.pipeline?.artifacts || {}).join(" | "),
            state: journeyPipelineData?.pipeline?.artifacts ? "pass" : "warn",
          },
        ],
        decision: "Decision: the evidence package is ready for review, audit, or adoption demos.",
      },
    };
  }

  throw new Error(`Unknown journey step: ${stepId}`);
}

async function runJourneyStep() {
  const step = journeyStepById(journeyCurrentStep);
  const actionKey = journeyActionKey(step.id);
  setJourneyStatus("running", "Running");
  $("journey-artifact").textContent = "Waiting for platform response.";
  $("journey-output").textContent = `Running Step ${step.number}: ${step.title}...`;
  renderJourneyPlain(step, {
    plain: {
      title: `Running Step ${step.number}: ${step.title}`,
      summary: "The platform is working on this step now. The result will appear here before you move to the next step.",
      checks: [
        {
          title: "Current action",
          detail: step.button,
          state: "info",
        },
        {
          title: "Expected result",
          detail: step.creates,
          state: "info",
        },
      ],
      decision: "Waiting for platform response.",
    },
  });
  setBusy(true);
  try {
    requirePreviousJourneySteps(step.id);
    const result = await executeJourneyStep(step.id);
    completeJourneyStep(step, { ...result, status: result.status || "pass" });
  } catch (error) {
    const message = error.message || String(error);
    if (actionKey) failAction(actionKey, message);
    const prerequisiteBlocked = message.includes("first") || message.includes("depends on evidence");
    completeJourneyStep(step, {
      status: "fail",
      statusLabel: prerequisiteBlocked ? "Needs prior step" : "Blocked",
      artifact: message,
      summary: message,
      output: message,
      plain: {
        title: prerequisiteBlocked ? "This step needs an earlier artifact" : "Why did this step stop?",
        summary: message,
        checks: [
          {
            title: "Blocked step",
            detail: `Step ${step.number}: ${step.title}`,
            state: "fail",
          },
          {
            title: "What to do next",
            detail: prerequisiteBlocked ? "Run the earlier step shown in the message, then come back here." : "Fix the failed evidence before continuing.",
            state: "warn",
          },
        ],
        decision: prerequisiteBlocked ? "Decision: waiting for the previous artifact. No device action was taken." : "Decision: blocked. The platform did not move to a later action.",
      },
    });
  } finally {
    setBusy(false);
    renderJourneyStep();
  }
}

function resetJourneyView() {
  Object.keys(journeyArtifacts).forEach((key) => delete journeyArtifacts[key]);
  journeyCompletedSteps.clear();
  journeyFailedSteps.clear();
  journeyDefinedRequest = null;
  journeyCurrentStep = "define";
  $("journey-journal").innerHTML = '<article class="journey-empty">Run a step to see the artifact path, result, and next safe action.</article>';
  renderJourneyStep();
}

function openConsoleFromJourney() {
  const detailByStep = {
    define: "intent",
    source: "source",
    intent: "intent",
    gitops: "gitops",
    template: "config",
    candidate: "config",
    validation: "checks",
    dryrun: "lab",
    apply: "lab",
    rollback: "lab",
    evidence: "jobs",
  };
  switchMode("console");
  setDetailsVisible(true);
  selectDetail(detailByStep[journeyCurrentStep] || "intent", false);
}

function selectDetail(name, log = true) {
  document.querySelectorAll(".detail-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.detail === name);
  });
  document.querySelectorAll(".detail-output").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `detail-${name}`);
  });
  if (log) {
    completeAction("select-detail", {
      status: "info",
      actual: `Showing ${name} evidence.`,
      evidence: "No network or backend state changed.",
    });
  }
}

document.querySelectorAll("#change-form input").forEach((input) => {
  input.addEventListener("input", requestChanged);
});

document.querySelectorAll(".detail-tab").forEach((button) => {
  button.addEventListener("click", () => selectDetail(button.dataset.detail));
});

document.querySelectorAll(".mode-tab").forEach((button) => {
  button.addEventListener("click", () => switchMode(button.dataset.mode));
});

document.querySelectorAll(".journey-step").forEach((button) => {
  button.addEventListener("click", () => selectJourneyStep(button.dataset.journeyStep));
});

$("run-safety").addEventListener("click", checkSafety);
$("run-dry-run").addEventListener("click", () => runLabAction("dry-run"));
$("run-apply").addEventListener("click", () => runLabAction("apply"));
$("run-rollback").addEventListener("click", () => runLabAction("rollback"));
$("journey-run-step").addEventListener("click", runJourneyStep);
$("journey-open-console").addEventListener("click", openConsoleFromJourney);
$("journey-reset").addEventListener("click", resetJourneyView);
$("refresh-platform").addEventListener("click", refreshPlatform);
$("show-source-of-truth").addEventListener("click", showSourceOfTruth);
$("show-adapter-matrix").addEventListener("click", showAdapterMatrix);
$("run-discovery").addEventListener("click", runDiscovery);
$("import-discovered-device").addEventListener("click", importDiscoveredDevice);
$("show-workflow-rules").addEventListener("click", showWorkflowRules);
$("show-gitops-plan").addEventListener("click", showGitOpsPlan);
$("show-conformance").addEventListener("click", showConformance);
$("show-verification-catalog").addEventListener("click", showVerificationCatalog);
$("run-drift-check").addEventListener("click", runDriftCheck);
$("show-scale-plan").addEventListener("click", showScalePlan);
$("show-jobs").addEventListener("click", showJobs);
$("show-device-state").addEventListener("click", showDeviceState);
$("verify-vlan-state").addEventListener("click", verifyVlanState);
$("ask-assistant").addEventListener("click", askAssistant);
$("clear-journal").addEventListener("click", clearJournal);
$("toggle-details").addEventListener("click", () => {
  setDetailsVisible($("technical-details").classList.contains("hidden"), true);
});

syncRequestedChange();
resetProofGates();
setDetailsVisible(false);
renderLocks();
renderJourneyStep();
refreshPlatform({ log: false });
