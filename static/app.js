const $ = (id) => document.getElementById(id);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const appState = {
  view: "home",
  artifact: "overview",
  health: null,
  git: null,
  source: null,
  rezHealth: null,
  rezPlatforms: null,
  discovery: null,
  discoveryCandidate: null,
  plan: null,
  gitPlan: null,
  dryRun: null,
  apply: null,
  verify: null,
  rollback: null,
  changeLive: false,
  jobs: null,
  workflow: null,
};

function formatJson(value) {
  return JSON.stringify(value, null, 2);
}

function apiError(data, fallback) {
  if (!data || data.detail == null) return fallback;
  return typeof data.detail === "string" ? data.detail : formatJson(data.detail);
}

async function getJson(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok) throw new Error(apiError(data, response.statusText));
  return data;
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(apiError(data, response.statusText));
  return data;
}

function setRunState(label, status = "info") {
  const el = $("run-state-label");
  el.textContent = label;
  el.className = status === "pass" ? "state-pass" : status === "fail" ? "state-fail" : status === "warn" ? "state-warn" : "";
}

function setOutcome({ state = "Info", status = "info", title, summary, expected, actual, artifact, device, next }) {
  const stateEl = $("outcome-state");
  stateEl.textContent = state;
  stateEl.className = status === "pass" ? "state-pass" : status === "fail" ? "state-fail" : status === "warn" ? "state-warn" : "";
  $("outcome-title").textContent = title;
  $("outcome-summary").textContent = summary;
  $("outcome-expected").textContent = expected;
  $("outcome-actual").textContent = actual;
  $("outcome-artifact").textContent = artifact;
  $("outcome-device").textContent = device;
  $("next-action").textContent = next;
}

function startOutcome(title, expected) {
  setOutcome({
    state: "Running",
    title,
    summary: "The platform is running this UI action now.",
    expected,
    actual: "Waiting for platform response.",
    artifact: "Pending.",
    device: "No committed config change unless this step explicitly says apply.",
    next: "Wait for the result.",
  });
}

function failOutcome(title, error, next = "Review the error, then retry the same step.") {
  setOutcome({
    state: "Failed",
    status: "fail",
    title,
    summary: "The platform stopped at this step.",
    expected: "Complete the requested action safely.",
    actual: error.message || String(error),
    artifact: "No later artifact was created.",
    device: "No later device action was unlocked.",
    next,
  });
}

function setView(view) {
  appState.view = view;
  $$(".view").forEach((panel) => panel.classList.toggle("active", panel.id === `view-${view}`));
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  const titles = {
    home: ["Define, plan, validate, apply, verify.", "A simple network-as-code flow using your Git repo, local YAML source of truth, Rez discovery, and the Arista ORB lab."],
    setup: ["Set up the workspace.", "Check Git, source of truth, adapters, and lab reachability before making a change."],
    inventory: ["Discover and trust devices.", "Use Rez read adapters to discover the Arista lab switch, then import the reviewed device record."],
    desired: ["Create desired state.", "Describe the network outcome in a simple form and let the platform create YAML and config."],
    plan: ["Preview exact impact.", "Review the Terraform-style plan, generated commands, and blast radius before any device write."],
    validate: ["Validate before apply.", "Policy checks and lab dry-run proof must pass before apply is unlocked."],
    apply: ["Apply and verify.", "Commit only after validation and dry-run proof, then prove live state."],
    evidence: ["Review the evidence.", "Inspect every artifact created or inspected from the UI."],
  };
  $("view-title").textContent = titles[view][0];
  $("view-subtitle").textContent = titles[view][1];
  if (view === "evidence") renderEvidence();
}

function changePayload() {
  const form = new FormData($("change-form"));
  return {
    site: String(form.get("site") || ""),
    device_id: String(form.get("device_id") || ""),
    vlan_id: Number(form.get("vlan_id")),
    name: String(form.get("name") || ""),
    subnet: String(form.get("subnet") || ""),
    purpose: String(form.get("purpose") || ""),
    requested_by: String(form.get("requested_by") || ""),
    pci_reachable: form.get("pci_reachable") === "on",
  };
}

function discoveryPayload() {
  const form = new FormData($("discover-form"));
  return {
    host: String(form.get("host") || ""),
    platform: String(form.get("platform") || ""),
    username: String(form.get("username") || ""),
    password: String(form.get("password") || ""),
    device_id: String(form.get("device_id") || ""),
    site: String(form.get("site") || ""),
    groups: [],
    port: 22,
  };
}

function commandListFromText(config) {
  return String(config || "")
    .split("\n")
    .map((line) => line.trimEnd())
    .filter((line) => line.trim());
}

function commandListBlock(commands, empty = "No commands available.") {
  if (!commands.length) return empty;
  return commands.map((command) => `$ ${command}`).join("\n");
}

function transcriptFromLab(data) {
  const result = data?.result || data || {};
  const evidence = result.evidence || {};
  const session = evidence.session || {};
  return evidence.transcript || session.transcript || [];
}

function transcriptText(data, fallback = "No device transcript available.") {
  const transcript = transcriptFromLab(data);
  if (!transcript.length) return fallback;
  return transcript.map((entry) => `$ ${entry.command}\n${entry.output || ""}`.trim()).join("\n\n");
}

function renderHome() {
  $("home-git").textContent = appState.git?.available ? "Ready" : "Needs setup";
  $("home-sot").textContent = appState.source ? `${appState.source.summary?.device_count || 0} devices` : "Unknown";
  $("home-rez").textContent = appState.rezHealth?.ok ? `${appState.rezHealth.platform_count} platforms` : "Unavailable";
  $("home-lab").textContent = appState.health?.lab?.ok ? "Reachable" : "Local only";
  $("sidebar-workspace").textContent = appState.git?.branch || "main";
  $("sidebar-lab").textContent = appState.health?.lab?.ok ? "Arista lab reachable" : "Lab not reachable from this runtime";
}

function renderSetup() {
  const git = appState.git || {};
  $("setup-git-copy").textContent = git.available
    ? `Git is ready on branch ${git.branch || "unknown"}. Remote: ${git.remote || "not configured"}.`
    : "Git is not initialized for this workspace yet.";
  $("setup-git-commands").textContent = commandListBlock(git.commands || []);

  const source = appState.source || {};
  $("setup-sot-copy").textContent = source.ok
    ? `Local YAML source of truth is active with ${source.summary?.device_count || 0} devices.`
    : "Source of truth is not loaded.";
  $("setup-sot-summary").textContent = source.ok ? formatJson(source.summary) : "Unavailable";

  const rez = appState.rezHealth || {};
  $("setup-rez-copy").textContent = rez.ok
    ? `Rez driver registry loaded from ${rez.root}.`
    : `Rez drivers unavailable: ${rez.error || "unknown error"}`;
  $("setup-rez-summary").textContent = appState.rezPlatforms ? formatJson(appState.rezPlatforms.platforms) : "Unavailable";

  const lab = appState.health?.lab || {};
  $("setup-lab-copy").textContent = lab.ok
    ? "Containerlab is reachable from this runtime."
    : "Containerlab is not reachable from this runtime. Use the ORB URL for lab actions.";
  $("setup-lab-summary").textContent = lab.ok ? lab.stdout || "Lab reachable." : lab.message || lab.stderr || "Unavailable";
}

function renderInventory() {
  const list = $("inventory-table");
  const devices = appState.source?.devices || [];
  if (!devices.length) {
    list.innerHTML = '<article class="device-row"><strong>No devices loaded</strong><p>Check source of truth.</p></article>';
    return;
  }
  list.innerHTML = devices
    .map(
      (device) => `
        <article class="device-row">
          <div>
            <strong>${device.id}</strong>
            <p>${device.platform} at ${device.host}:${device.port}</p>
          </div>
          <span>${device.site || "unassigned"}</span>
        </article>
      `
    )
    .join("");
}

function renderDesiredSummary() {
  const payload = changePayload();
  $("desired-title").textContent = `Add VLAN ${payload.vlan_id} to ${payload.device_id}`;
  $("desired-summary").textContent = `Desired state: VLAN ${payload.vlan_id} named ${payload.name} at ${payload.site}.`;
}

function renderPlan() {
  const plan = appState.plan;
  if (!plan) {
    $("plan-action").textContent = "No plan yet";
    $("plan-device").textContent = "-";
    $("plan-risk").textContent = "Unknown";
    $("plan-writes").textContent = "None";
    $("plan-summary-text").textContent = "Create a desired state first.";
    $("plan-commands").textContent = "No commands generated yet.";
    return;
  }
  const payload = changePayload();
  const pipeline = plan.pipeline;
  const commands = commandListFromText(pipeline.render.config);
  $("plan-action").textContent = `Add VLAN ${payload.vlan_id}`;
  $("plan-device").textContent = payload.device_id;
  $("plan-risk").textContent = pipeline.validation.status === "pass" ? "Low for lab" : "Blocked";
  $("plan-writes").textContent = "None during plan";
  $("plan-summary-text").textContent = [
    "Netcode plan",
    "",
    `+ VLAN ${payload.vlan_id} (${payload.name})`,
    `  target: ${payload.device_id}`,
    `  site: ${payload.site}`,
    `  subnet: ${payload.subnet}`,
    "",
    "Device writes: none",
    "Next: review validation, then dry-run in Arista EOS config session.",
  ].join("\n");
  $("plan-commands").textContent = commandListBlock(commands);
  $("desired-yaml").textContent = pipeline.intent_yaml;
}

function renderValidation() {
  const checks = appState.plan?.pipeline?.validation?.checks || [];
  const list = $("validation-list");
  if (!checks.length) {
    list.innerHTML = '<article class="check-item"><strong>No validation yet</strong><p>Create a plan first.</p></article>';
  } else {
    list.innerHTML = checks
      .map(
        (check) => `
          <article class="check-item ${check.status}">
            <strong>${check.status.toUpperCase()}: ${check.title}</strong>
            <p>${check.message}</p>
          </article>
        `
      )
      .join("");
  }
  $("git-plan").textContent = appState.gitPlan ? formatJson(appState.gitPlan) : "Create a plan first.";
  $("dryrun-proof").textContent = appState.dryRun ? formatJson(appState.dryRun) : "Run dry-run after validation is reviewed.";
  $("run-dry-run").disabled = !(appState.plan?.ok);
}

function setGate(id, state, label) {
  const gate = $(id);
  gate.className = state;
  gate.querySelector("strong").textContent = label;
}

function renderApply() {
  setGate("gate-plan", appState.plan ? "pass" : "warn", appState.plan ? "Planned" : "Waiting");
  setGate("gate-validation", appState.plan?.ok ? "pass" : appState.plan ? "fail" : "warn", appState.plan?.ok ? "Passed" : appState.plan ? "Blocked" : "Waiting");
  setGate("gate-dryrun", appState.dryRun?.ok ? "pass" : appState.dryRun ? "fail" : "warn", appState.dryRun?.ok ? "Passed" : appState.dryRun ? "Failed" : "Waiting");
  if (appState.rollback?.ok && !appState.changeLive) {
    setGate("gate-verify", "pass", "Rolled back");
  } else {
    setGate("gate-verify", appState.verify?.ok || appState.apply?.ok ? "pass" : appState.verify ? "fail" : "warn", appState.verify?.ok || appState.apply?.ok ? "Verified" : appState.verify ? "Failed" : "Waiting");
  }
  $("apply-change").disabled = !(appState.plan?.ok && appState.dryRun?.ok);
  $("verify-change").disabled = !(appState.apply?.ok && appState.changeLive);
  $("rollback-change").disabled = !(appState.apply?.ok && appState.changeLive);
  if (appState.rollback) {
    $("apply-transcript").textContent = transcriptText(appState.rollback, formatJson(appState.rollback));
  } else if (appState.apply) {
    $("apply-transcript").textContent = transcriptText(appState.apply, formatJson(appState.apply));
  } else if (appState.dryRun) {
    $("apply-transcript").textContent = transcriptText(appState.dryRun, formatJson(appState.dryRun));
  } else {
    $("apply-transcript").textContent = "No device command has been committed.";
  }
}

function renderAll() {
  renderHome();
  renderSetup();
  renderInventory();
  renderDesiredSummary();
  renderPlan();
  renderValidation();
  renderApply();
  renderEvidence();
}

async function checkWorkspace({ silent = false } = {}) {
  if (!silent) startOutcome("Check workspace", "Load Git, source of truth, Rez adapter, lab, and job status.");
  try {
    const [health, git, source, rezHealth, rezPlatforms, jobs] = await Promise.all([
      getJson("/api/health"),
      getJson("/api/git/status"),
      getJson("/api/source-of-truth"),
      getJson("/api/adapters/rez/health"),
      getJson("/api/adapters/rez/platforms"),
      getJson("/api/jobs"),
    ]);
    appState.health = health;
    appState.git = git;
    appState.source = source;
    appState.rezHealth = rezHealth;
    appState.rezPlatforms = rezPlatforms;
    appState.jobs = jobs;
    setRunState("Workspace checked", health.lab?.ok ? "pass" : "warn");
    renderAll();
    if (!silent) {
      setOutcome({
        state: health.lab?.ok ? "Passed" : "Review",
        status: health.lab?.ok ? "pass" : "warn",
        title: "Workspace check complete.",
        summary: "Git, source of truth, Rez adapters, and lab status were loaded.",
        expected: "Confirm the platform is ready before making a network change.",
        actual: `${git.available ? "Git ready" : "Git needs setup"}. ${source.summary?.device_count || 0} source-of-truth devices. Rez platforms: ${rezHealth.platform_count || 0}.`,
        artifact: "Workspace status loaded from API.",
        device: "No device config was changed.",
        next: "Discover the lab device or create desired state.",
      });
    }
  } catch (error) {
    failOutcome("Workspace check failed.", error);
  }
}

async function discoverDevice() {
  startOutcome("Discover device", "Use Rez read/state collection to identify the device and prepare a source-of-truth candidate.");
  try {
    const data = await postJson("/api/discovery/scan", discoveryPayload());
    appState.discovery = data;
    appState.discoveryCandidate = data.source_of_truth_candidate || null;
    $("save-discovered-device").disabled = !appState.discoveryCandidate;
    $("discovery-title").textContent = data.ok ? `${data.platform} device discovered` : "Discovery failed";
    $("discovery-summary").textContent = data.ok
      ? `${data.state_summary?.hostname || data.host} at ${data.host}. Import candidate is ready.`
      : data.error || "Rez could not collect state.";
    appState.artifact = "overview";
    renderEvidence();
    setOutcome({
      state: data.ok ? "Passed" : "Failed",
      status: data.ok ? "pass" : "fail",
      title: data.ok ? "Device discovered." : "Device discovery failed.",
      summary: data.ok ? "Rez read the device and created a source-of-truth candidate." : "Rez could not complete discovery.",
      expected: "Collect live device facts without changing config.",
      actual: data.ok ? `Adapter ${data.adapter} collected ${data.state_summary?.interfaces || 0} interfaces and ${data.state_summary?.vlans || 0} VLANs.` : data.error || "Discovery failed.",
      artifact: data.ok ? "Discovery result and source-of-truth candidate." : "Discovery error.",
      device: "No device config was changed.",
      next: data.ok ? "Save the reviewed candidate to source of truth." : "Check IP, credentials, vendor, or lab reachability.",
    });
  } catch (error) {
    failOutcome("Discovery failed.", error);
  }
}

async function saveDiscoveredDevice() {
  if (!appState.discoveryCandidate) {
    failOutcome("No discovery candidate.", new Error("Run discovery first."));
    return;
  }
  startOutcome("Save to source of truth", "Write the reviewed device record into local YAML source of truth.");
  try {
    const data = await postJson("/api/source-of-truth/devices/import", { candidate: appState.discoveryCandidate });
    appState.source = await getJson("/api/source-of-truth");
    renderAll();
    setOutcome({
      state: data.ok ? "Passed" : "Failed",
      status: data.ok ? "pass" : "fail",
      title: data.ok ? "Source of truth updated." : "Source-of-truth import failed.",
      summary: data.message || "Import completed.",
      expected: "Save the device metadata, not the discovery password.",
      actual: data.message || "Inventory write completed.",
      artifact: data.inventory || "inventories/lab.yaml",
      device: "No device config was changed.",
      next: "Create desired state for the lab device.",
    });
  } catch (error) {
    failOutcome("Source-of-truth import failed.", error);
  }
}

async function createPlan() {
  startOutcome("Create plan", "Create YAML intent, render Arista EOS config, and run static policy checks. No device contact.");
  try {
    const data = await postJson("/api/wizard/add-vlan", changePayload());
    appState.plan = data;
    appState.dryRun = null;
    appState.apply = null;
    appState.verify = null;
    appState.rollback = null;
    appState.changeLive = false;
    if (data.intent_path) {
      appState.gitPlan = await postJson("/api/gitops/plan", {
        intent_path: data.intent_path,
        device_id: changePayload().device_id,
        change_id: data.change?.id || null,
      });
    }
    renderAll();
    setRunState(data.ok ? "Planned" : "Blocked", data.ok ? "pass" : "fail");
    setView("plan");
    setOutcome({
      state: data.ok ? "Passed" : "Failed",
      status: data.ok ? "pass" : "fail",
      title: data.ok ? "Plan created." : "Plan blocked by validation.",
      summary: "The platform created the desired-state YAML and rendered candidate config.",
      expected: "Generate a reviewable plan without touching the device.",
      actual: `${data.pipeline.validation.checks.length} checks returned ${data.pipeline.validation.status}.`,
      artifact: data.intent_path,
      device: "No device config was changed.",
      next: data.ok ? "Review validation, then run lab dry-run." : "Fix the request or policy issue before dry-run.",
    });
  } catch (error) {
    failOutcome("Plan creation failed.", error);
  }
}

function reviewValidation() {
  if (!appState.plan) {
    failOutcome("No plan available.", new Error("Create desired state first."));
    return;
  }
  renderValidation();
  setView("validate");
  setOutcome({
    state: appState.plan.ok ? "Passed" : "Failed",
    status: appState.plan.ok ? "pass" : "fail",
    title: "Validation reviewed.",
    summary: "Static checks are visible and the Git review plan is attached.",
    expected: "Inspect policy and generated config guardrails before any device contact.",
    actual: `${appState.plan.pipeline.validation.checks.length} validation checks reviewed.`,
    artifact: appState.plan.pipeline.artifacts?.report_markdown_path || "Validation report.",
    device: "No device config was changed.",
    next: appState.plan.ok ? "Run lab dry-run." : "Fix validation failures first.",
  });
}

async function runDryRun() {
  if (!appState.plan?.intent_path) {
    failOutcome("Dry-run blocked.", new Error("Create a plan first."));
    return;
  }
  startOutcome("Run lab dry-run", "Open EOS config session, load candidate, collect diff, then abort. No commit.");
  try {
    const data = await postJson("/api/lab/dry-run", {
      intent_path: appState.plan.intent_path,
      device_id: changePayload().device_id,
      change_id: appState.plan.change?.id || null,
    });
    appState.dryRun = data;
    renderAll();
    setRunState(data.ok ? "Dry-run passed" : "Dry-run failed", data.ok ? "pass" : "fail");
    setView("apply");
    setOutcome({
      state: data.ok ? "Passed" : "Failed",
      status: data.ok ? "pass" : "fail",
      title: data.ok ? "Lab dry-run passed." : "Lab dry-run failed.",
      summary: data.result?.message || "Dry-run completed.",
      expected: "EOS accepts candidate config in an aborted config session.",
      actual: data.result?.message || "Dry-run returned.",
      artifact: data.job ? `Job ${data.job.id}` : "Dry-run result.",
      device: "No config was committed. Dry-run aborted the session.",
      next: data.ok ? "Apply is unlocked for the Arista lab." : "Review dry-run transcript before retry.",
    });
  } catch (error) {
    failOutcome("Dry-run failed.", error);
  }
}

async function applyChange() {
  if (!appState.dryRun?.ok) {
    failOutcome("Apply blocked.", new Error("Run a passing dry-run first."));
    return;
  }
  startOutcome("Apply in Arista lab", "Commit the validated candidate, then verify VLAN state.");
  try {
    const data = await postJson("/api/lab/apply", {
      intent_path: appState.plan.intent_path,
      device_id: changePayload().device_id,
      change_id: appState.plan.change?.id || null,
    });
    appState.apply = data;
    appState.changeLive = Boolean(data.ok);
    appState.rollback = null;
    renderAll();
    setRunState(data.ok ? "Applied" : "Apply failed", data.ok ? "pass" : "fail");
    setOutcome({
      state: data.ok ? "Passed" : "Failed",
      status: data.ok ? "pass" : "fail",
      title: data.ok ? "Lab change applied and verified." : "Apply failed.",
      summary: data.result?.message || "Apply completed.",
      expected: "Commit only after plan, validation, and dry-run passed.",
      actual: data.result?.message || "Apply returned.",
      artifact: data.job ? `Job ${data.job.id}` : "Apply result.",
      device: data.ok ? "Candidate config was committed in the Arista lab." : "Commit did not complete safely.",
      next: data.ok ? "Run live-state verification or rollback." : "Review apply transcript.",
    });
  } catch (error) {
    failOutcome("Apply failed.", error);
  }
}

async function verifyChange() {
  if (!appState.apply?.ok) {
    failOutcome("Verify blocked.", new Error("Apply the lab change first."));
    return;
  }
  startOutcome("Verify live state", "Collect device state through Rez and prove the VLAN exists.");
  try {
    const payload = changePayload();
    const data = await postJson("/api/verify/vlan", {
      device_id: payload.device_id,
      vlan_id: payload.vlan_id,
      name: payload.name,
      present: true,
    });
    appState.verify = data;
    renderAll();
    setOutcome({
      state: data.ok ? "Passed" : "Failed",
      status: data.ok ? "pass" : "fail",
      title: data.ok ? "Live state verified." : "Live state verification failed.",
      summary: data.verification?.message || "Verification completed.",
      expected: "Live device state matches desired state.",
      actual: data.verification?.message || "Verification returned.",
      artifact: `Rez state via ${data.state?.adapter || "unknown adapter"}.`,
      device: "No config was changed during verification.",
      next: "Review evidence or rollback the lab change.",
    });
  } catch (error) {
    failOutcome("Verification failed.", error);
  }
}

async function rollbackChange() {
  if (!appState.apply?.ok) {
    failOutcome("Rollback blocked.", new Error("Apply the lab change first."));
    return;
  }
  startOutcome("Rollback lab change", "Commit no-vlan rollback and verify the VLAN is absent.");
  try {
    const data = await postJson("/api/lab/rollback", {
      intent_path: appState.plan.intent_path,
      device_id: changePayload().device_id,
      change_id: appState.plan.change?.id || null,
    });
    appState.rollback = data;
    if (data.ok) appState.changeLive = false;
    renderAll();
    setRunState(data.ok ? "Rolled back" : "Rollback failed", data.ok ? "pass" : "fail");
    setOutcome({
      state: data.ok ? "Passed" : "Failed",
      status: data.ok ? "pass" : "fail",
      title: data.ok ? "Rollback verified." : "Rollback failed.",
      summary: data.result?.message || "Rollback completed.",
      expected: "Remove the lab VLAN and prove it is absent.",
      actual: data.result?.message || "Rollback returned.",
      artifact: data.job ? `Job ${data.job.id}` : "Rollback result.",
      device: data.ok ? "Rollback config was committed in the Arista lab." : "Rollback did not complete safely.",
      next: "Review evidence.",
    });
  } catch (error) {
    failOutcome("Rollback failed.", error);
  }
}

async function refreshEvidence() {
  startOutcome("Refresh evidence", "Load latest jobs, workflow events, and Git plan.");
  try {
    appState.jobs = await getJson("/api/jobs");
    if (appState.plan?.change?.id) {
      appState.workflow = await getJson(`/api/workflow/change/${appState.plan.change.id}`);
      appState.gitPlan = await postJson("/api/gitops/plan", {
        intent_path: appState.plan.intent_path,
        device_id: changePayload().device_id,
        change_id: appState.plan.change.id,
      });
    }
    renderEvidence();
    setOutcome({
      state: "Passed",
      status: "pass",
      title: "Evidence refreshed.",
      summary: "Latest jobs, workflow events, reports, and Git review data are visible.",
      expected: "Collect audit evidence for the current MVP flow.",
      actual: `${appState.jobs?.jobs?.length || 0} job records loaded.`,
      artifact: "Evidence view updated.",
      device: "No device config was changed.",
      next: "Use the evidence tabs to inspect artifacts.",
    });
  } catch (error) {
    failOutcome("Evidence refresh failed.", error);
  }
}

function evidencePayload() {
  return {
    workspace: appState.health?.workspace,
    run_state: $("run-state-label").textContent,
    source_of_truth: appState.source?.summary,
    current_change: appState.plan?.change || null,
    intent_path: appState.plan?.intent_path || null,
    reports: appState.plan?.pipeline?.artifacts || null,
    discovery: appState.discovery
      ? {
          ok: appState.discovery.ok,
          platform: appState.discovery.platform,
          adapter: appState.discovery.adapter,
          state_summary: appState.discovery.state_summary,
          safety: appState.discovery.safety,
        }
      : null,
    dry_run: appState.dryRun
      ? {
          ok: appState.dryRun.ok,
          job: appState.dryRun.job,
          message: appState.dryRun.result?.message,
        }
      : null,
    apply: appState.apply
      ? {
          ok: appState.apply.ok,
          job: appState.apply.job,
          message: appState.apply.result?.message,
        }
      : null,
    verify: appState.verify || null,
    rollback: appState.rollback
      ? {
          ok: appState.rollback.ok,
          job: appState.rollback.job,
          message: appState.rollback.result?.message,
        }
      : null,
  };
}

function renderEvidence() {
  if (!$("evidence-output")) return;
  const artifact = appState.artifact || "overview";
  $$(".evidence-tab").forEach((button) => button.classList.toggle("active", button.dataset.artifact === artifact));
  const pipeline = appState.plan?.pipeline;
  const outputs = {
    overview: formatJson(evidencePayload()),
    intent: pipeline?.intent_yaml || "No YAML intent yet.",
    config: pipeline?.render?.config || "No generated commands yet.",
    validation: pipeline?.validation ? formatJson(pipeline.validation) : "No validation report yet.",
    lab: formatJson({
      dry_run: appState.dryRun,
      apply: appState.apply,
      verify: appState.verify,
      rollback: appState.rollback,
    }),
    git: appState.gitPlan ? formatJson(appState.gitPlan) : appState.git ? formatJson(appState.git) : "No Git evidence yet.",
    jobs: appState.jobs ? formatJson(appState.jobs) : "No jobs loaded yet.",
  };
  $("evidence-output").textContent = outputs[artifact] || outputs.overview;
}

function resetChangeProof() {
  appState.plan = null;
  appState.gitPlan = null;
  appState.dryRun = null;
  appState.apply = null;
  appState.verify = null;
  appState.rollback = null;
  appState.changeLive = false;
  setRunState("Draft");
  renderAll();
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $$("[data-go]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.go)));
  $$(".evidence-tab").forEach((button) =>
    button.addEventListener("click", () => {
      appState.artifact = button.dataset.artifact;
      renderEvidence();
    })
  );
  $("check-workspace").addEventListener("click", () => checkWorkspace());
  $("discover-device").addEventListener("click", discoverDevice);
  $("save-discovered-device").addEventListener("click", saveDiscoveredDevice);
  $("create-plan").addEventListener("click", createPlan);
  $("review-validation").addEventListener("click", reviewValidation);
  $("run-dry-run").addEventListener("click", runDryRun);
  $("apply-change").addEventListener("click", applyChange);
  $("verify-change").addEventListener("click", verifyChange);
  $("rollback-change").addEventListener("click", rollbackChange);
  $("refresh-evidence").addEventListener("click", refreshEvidence);
  $$("#change-form input").forEach((input) => input.addEventListener("input", resetChangeProof));
}

bindEvents();
renderAll();
checkWorkspace({ silent: true });
