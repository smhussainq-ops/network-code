const $ = (id) => document.getElementById(id);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const EMPTY_CHANGE_TYPE = {
  id: "",
  label: "Select change type",
  outcome: "Load configuration to show editable desired-state workflows.",
  risk: "Unknown",
  lab_write_supported: false,
  production_write_supported: false,
  fields: [],
};

const appState = {
  view: "home",
  artifact: "overview",
  selectedChangeType: "add_vlan",
  formValues: {},
  catalog: null,
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
  audit: null,
  drift: null,
  uiConfig: null,
  uiConfigPath: "",
  configHistory: [],
  configApplied: false,
  gitBranches: null,
  activeChangeId: "",
  lastCommit: null,
  lastPush: null,
  changeRecord: null,
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

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
    home: ["Define, plan, validate, apply, verify.", "A Terraform-style network-as-code flow using Git, source of truth, Rez discovery, typed intents, validation, and audited Arista lab proof."],
    setup: ["Set up the workspace.", "Check Git, source of truth, adapters, and lab reachability before making a change."],
    inventory: ["Discover and trust devices.", "Use Rez read adapters to discover devices, then import reviewed records into source of truth."],
    desired: ["Create desired state.", "Choose the network outcome, fill the intent fields, and let the platform create YAML and candidate config."],
    plan: ["Preview exact impact.", "Review the Terraform-style plan, generated commands, affected devices, risk, and apply gate."],
    validate: ["Validate before apply.", "Policy checks and lab dry-run proof must pass before apply is unlocked."],
    apply: ["Apply and verify.", "Commit only after validation and dry-run proof, then prove live state and keep rollback available."],
    drift: ["Detect drift.", "Compare desired state and live state without changing the network."],
    evidence: ["Review the evidence.", "Inspect every artifact, audit event, and command session created from the UI."],
  };
  $("view-title").textContent = titles[view][0];
  $("view-subtitle").textContent = titles[view][1];
  if (view === "evidence") renderEvidence();
  if (view === "drift") renderDrift();
}

function getPath(object, path, fallback = "") {
  return path.split(".").reduce((cursor, key) => (cursor && Object.prototype.hasOwnProperty.call(cursor, key) ? cursor[key] : undefined), object) ?? fallback;
}

function setPath(object, path, value) {
  const keys = path.split(".");
  let cursor = object;
  keys.slice(0, -1).forEach((key) => {
    if (!cursor[key] || typeof cursor[key] !== "object" || Array.isArray(cursor[key])) cursor[key] = {};
    cursor = cursor[key];
  });
  cursor[keys[keys.length - 1]] = value;
}

function parseList(value) {
  if (Array.isArray(value)) return value.map(String).filter(Boolean);
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function cloneConfig() {
  return JSON.parse(JSON.stringify(appState.uiConfig || {}));
}

function changeTypeList() {
  if (appState.catalog?.change_types?.length) return appState.catalog.change_types;
  const changeTypes = getPath(appState.uiConfig || {}, "desired_state.change_types", {});
  return Object.entries(changeTypes).map(([id, item]) => ({ id, ...item }));
}

function catalogItem(id = appState.selectedChangeType) {
  return changeTypeList().find((item) => item.id === id) || changeTypeList()[0] || EMPTY_CHANGE_TYPE;
}

function localSchema(id = appState.selectedChangeType) {
  return catalogItem(id);
}

function renderChangeTypeGrid() {
  const catalog = changeTypeList();
  if (!catalog.length) {
    $("change-type-grid").innerHTML = '<article class="check-item"><strong>No workflows loaded</strong><p>Open Setup and load or save configuration.</p></article>';
    return;
  }
  if (!catalog.some((item) => item.id === appState.selectedChangeType)) {
    appState.selectedChangeType = catalog[0].id;
  }
  $("change-type-grid").innerHTML = catalog
    .map((item) => {
      const active = item.id === appState.selectedChangeType ? "active" : "";
      const lab = item.lab_write_supported ? "Lab apply" : "Plan only";
      return `
        <button class="change-type-card ${active}" type="button" data-change-type="${escapeHtml(item.id)}">
          <strong>${escapeHtml(item.label)}</strong>
          <span>${escapeHtml(item.risk || "")}</span>
          <p>${escapeHtml(item.outcome || "")}</p>
          <em>${escapeHtml(lab)}</em>
        </button>
      `;
    })
    .join("");
  $$("#change-type-grid [data-change-type]").forEach((button) =>
    button.addEventListener("click", () => {
      selectChangeType(button.dataset.changeType);
    })
  );
}

function renderDynamicFields() {
  const schema = localSchema();
  $("dynamic-title").textContent = `${schema.label} details`;
  $("dynamic-fields").innerHTML = (schema.fields || []).map(normalizeField).map(renderField).join("");
  $$("#dynamic-fields input, #dynamic-fields select, #dynamic-fields textarea").forEach((input) =>
    input.addEventListener("input", () => {
      storeDynamicValues();
      resetChangeProof();
    })
  );
}

function renderField(field) {
  const name = escapeHtml(field.name);
  const label = escapeHtml(field.label);
  const value = storedFieldValue(field);
  if (field.type === "checkbox") {
    return `<label class="check-row"><input name="${name}" type="checkbox" ${value ? "checked" : ""} /> ${label}</label>`;
  }
  if (field.type === "select") {
    const options = (field.options || []).map(optionPair).map(([optionValue, text]) => `<option value="${escapeHtml(optionValue)}" ${String(optionValue) === String(value) ? "selected" : ""}>${escapeHtml(text)}</option>`).join("");
    return `<label>${label}<select name="${name}">${options}</select></label>`;
  }
  if (field.type === "textarea") {
    return `<label class="wide">${label}<textarea name="${name}" placeholder="${escapeHtml(field.placeholder || "")}">${escapeHtml(value || "")}</textarea></label>`;
  }
  return `<label>${label}<input name="${name}" type="${escapeHtml(field.type || "text")}" value="${escapeHtml(value ?? "")}" min="${escapeHtml(field.min ?? "")}" max="${escapeHtml(field.max ?? "")}" placeholder="${escapeHtml(field.placeholder || "")}" /></label>`;
}

function normalizeField(field) {
  if (typeof field === "string") return { name: field, label: field, type: "text", value: "" };
  return {
    name: field.name || "field",
    label: field.label || field.name || "Field",
    type: field.type || "text",
    value: field.value ?? "",
    placeholder: field.placeholder || "",
    min: field.min,
    max: field.max,
    options: Array.isArray(field.options) ? field.options : [],
  };
}

function optionPair(option) {
  if (Array.isArray(option)) return [option[0], option[1] ?? option[0]];
  if (option && typeof option === "object") return [option.value ?? option.id ?? "", option.label ?? option.name ?? option.value ?? ""];
  return [option, option];
}

function storedFieldValue(field) {
  const values = appState.formValues[appState.selectedChangeType] || {};
  return Object.prototype.hasOwnProperty.call(values, field.name) ? values[field.name] : field.value;
}

function storeDynamicValues() {
  const schema = localSchema();
  const form = new FormData($("change-form"));
  const values = {};
  for (const rawField of schema.fields || []) {
    const field = normalizeField(rawField);
    if (field.type === "checkbox") {
      values[field.name] = form.get(field.name) === "on";
    } else if (field.type === "number") {
      const raw = form.get(field.name);
      values[field.name] = raw === null || raw === "" ? null : Number(raw);
    } else {
      values[field.name] = String(form.get(field.name) || "");
    }
  }
  appState.formValues[appState.selectedChangeType] = values;
  return values;
}

function readDynamicValues() {
  const values = storeDynamicValues();
  const form = new FormData($("change-form"));
  values.ticket_id = String(form.get("ticket_id") || "");
  return values;
}

function changePayload() {
  const form = new FormData($("change-form"));
  return {
    change_type: appState.selectedChangeType,
    site: String(form.get("site") || ""),
    device_id: String(form.get("device_id") || ""),
    requested_by: String(form.get("requested_by") || ""),
    values: readDynamicValues(),
  };
}

function selectedDeviceId() {
  const payload = changePayload();
  return payload.change_type === "site_device_intent" && payload.values.new_device_id ? String(payload.values.new_device_id) : payload.device_id;
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
    groups: parseList(form.get("groups")),
    port: Number(form.get("port") || getPath(appState.uiConfig || {}, "credentials.port", 22)),
  };
}

function setNamedField(form, name, value) {
  const field = form.elements[name];
  if (!field) return;
  if (field.type === "checkbox") {
    field.checked = Boolean(value);
  } else if (Array.isArray(value)) {
    field.value = value.join(",");
  } else {
    field.value = value ?? "";
  }
}

function fieldValue(field) {
  if (field.type === "checkbox") return field.checked;
  if (field.type === "number") return field.value === "" ? null : Number(field.value);
  return field.value;
}

function vendorOptions() {
  const configured = getPath(appState.uiConfig || {}, "discovery.vendor_options", []);
  if (Array.isArray(configured) && configured.length) return configured;
  const rez = appState.rezPlatforms?.platforms || [];
  return [["", "Auto detect with Rez"], ...rez.map((item) => [item.platform, item.platform])];
}

function renderDiscoveryVendorOptions(selected = "") {
  const select = $("discover-form").elements.platform;
  select.innerHTML = vendorOptions()
    .map(optionPair)
    .map(([value, label]) => `<option value="${escapeHtml(value)}" ${String(value) === String(selected) ? "selected" : ""}>${escapeHtml(label)}</option>`)
    .join("");
}

function applyConfigToOperationalForms({ force = false } = {}) {
  if (!appState.uiConfig || (appState.configApplied && !force)) return;
  const discover = $("discover-form");
  const discoveryDefaults = getPath(appState.uiConfig, "discovery.defaults", {});
  renderDiscoveryVendorOptions(discoveryDefaults.platform || "");
  ["host", "platform", "device_id", "site", "groups", "port", "username"].forEach((name) => {
    setNamedField(discover, name, discoveryDefaults[name] ?? getPath(appState.uiConfig, `credentials.${name}`, ""));
  });

  const change = $("change-form");
  const common = getPath(appState.uiConfig, "desired_state.common", {});
  ["site", "device_id", "requested_by", "ticket_id"].forEach((name) => setNamedField(change, name, common[name] || ""));
  const configuredType = getPath(appState.uiConfig, "desired_state.selected_change_type", "");
  const availableTypes = changeTypeList().map((item) => item.id);
  appState.selectedChangeType = availableTypes.includes(configuredType) ? configuredType : availableTypes[0] || "";
  appState.formValues = {};
  appState.configApplied = true;
}

function renderConfigPanel() {
  if (!$("platform-config-form")) return;
  $("config-path").textContent = appState.uiConfigPath ? `Saved at ${appState.uiConfigPath}` : "Configuration is loaded from the platform API.";
  const form = $("platform-config-form");
  if (!appState.uiConfig) return;
  Array.from(form.elements).forEach((field) => {
    if (!field.name) return;
    const value = field.name.endsWith(".groups") ? parseList(getPath(appState.uiConfig, field.name, [])) : getPath(appState.uiConfig, field.name, "");
    setNamedField(form, field.name, value);
  });
  $("config-json").value = formatJson(appState.uiConfig);
}

function configFromQuickForm() {
  const config = cloneConfig();
  const form = $("platform-config-form");
  Array.from(form.elements).forEach((field) => {
    if (!field.name) return;
    let value = fieldValue(field);
    if (field.name.endsWith(".groups")) value = parseList(value);
    setPath(config, field.name, value);
  });
  return config;
}

function syncQuickConfigToJson() {
  appState.uiConfig = configFromQuickForm();
  $("config-json").value = formatJson(appState.uiConfig);
}

async function reloadPlatformConfig({ silent = false, forceForms = true } = {}) {
  if (!silent) startOutcome("Reload configuration", "Read the saved UI configuration and re-render editable platform options.");
  try {
    const [config, catalog] = await Promise.all([getJson("/api/config/ui"), getJson("/api/desired-state/catalog")]);
    appState.uiConfig = config.config;
    appState.uiConfigPath = config.path;
    appState.configHistory = config.history || [];
    appState.catalog = catalog;
    appState.configApplied = false;
    applyConfigToOperationalForms({ force: forceForms });
    renderAll();
    if (!silent) {
      setOutcome({
        state: "Passed",
        status: "pass",
        title: "Configuration reloaded.",
        summary: "Editable UI settings were loaded from the platform configuration artifact.",
        expected: "Use saved Git, source-of-truth, discovery, workflow, and desired-state options.",
        actual: `${changeTypeList().length} desired-state workflows loaded from config.`,
        artifact: appState.uiConfigPath,
        device: "No device config was changed.",
        next: "Edit and save configuration, or create desired state.",
      });
    }
  } catch (error) {
    failOutcome("Configuration reload failed.", error);
  }
}

async function savePlatformConfig() {
  startOutcome("Save configuration", "Persist every editable UI option as a platform configuration artifact.");
  let config;
  try {
    config = JSON.parse($("config-json").value || "{}");
  } catch (error) {
    failOutcome("Configuration is not valid JSON.", error, "Fix the JSON syntax, then save again.");
    return;
  }
  try {
    const data = await postJson("/api/config/ui", { config });
    appState.uiConfig = data.config;
    appState.uiConfigPath = data.path;
    appState.configHistory = data.history || [];
    appState.catalog = await getJson("/api/desired-state/catalog");
    appState.configApplied = false;
    applyConfigToOperationalForms({ force: true });
    renderAll();
    setOutcome({
      state: "Passed",
      status: "pass",
      title: "Configuration saved.",
      summary: "The UI, discovery defaults, source-of-truth paths, workflow gates, and desired-state schemas now use the saved settings.",
      expected: "Make platform options editable and reusable.",
      actual: `Saved ${changeTypeList().length} workflow definitions to ${appState.uiConfigPath}.`,
      artifact: appState.uiConfigPath,
      device: "No device config was changed.",
      next: "Run workspace check or create a plan with the updated options.",
    });
  } catch (error) {
    failOutcome("Configuration save failed.", error);
  }
}

async function resetPlatformConfig() {
  startOutcome("Reset configuration", "Restore the editable UI configuration to platform defaults.");
  try {
    const data = await postJson("/api/config/ui/reset", {});
    appState.uiConfig = data.config;
    appState.uiConfigPath = data.path;
    appState.configHistory = data.history || [];
    appState.catalog = await getJson("/api/desired-state/catalog");
    appState.configApplied = false;
    applyConfigToOperationalForms({ force: true });
    renderAll();
    setOutcome({
      state: "Passed",
      status: "pass",
      title: "Configuration reset.",
      summary: "Default editable platform settings were restored and logged.",
      expected: "Return to a known-good Arista MVP configuration.",
      actual: `Reset saved at ${appState.uiConfigPath}.`,
      artifact: appState.uiConfigPath,
      device: "No device config was changed.",
      next: "Edit settings or run workspace check.",
    });
  } catch (error) {
    failOutcome("Configuration reset failed.", error);
  }
}

async function connectGitRepo() {
  const gitConfig = getPath(appState.uiConfig || {}, "git", {});
  const repoUrl = $("git-repo-url").value.trim() || gitConfig.repo_url || "";
  const baseBranch = $("git-base-branch").value.trim() || gitConfig.branch || "main";
  startOutcome("Connect Git repo", "Initialize this runtime workspace and attach the Git remote and base branch.");
  try {
    const data = await postJson("/api/git/setup", {
      repo_url: repoUrl,
      branch: baseBranch,
    });
    appState.git = data.status;
    appState.gitBranches = await getJson("/api/git/branches");
    renderAll();
    setOutcome({
      state: data.ok ? "Passed" : "Review",
      status: data.ok ? "pass" : "warn",
      title: data.ok ? "Git connected." : "Git setup needs review.",
      summary: data.ok
        ? "This workspace is now a Git repo connected to the configured remote."
        : "The platform attempted Git setup and captured the command results.",
      expected: "Create a reviewable repository path before network changes are pushed.",
      actual: (data.steps || []).map((step) => `${step.ok ? "OK" : "CHECK"}: ${step.command}${!step.ok && step.stderr ? ` - ${step.stderr}` : ""}`).join("\n"),
      artifact: data.workspace,
      device: "No device config was changed.",
      next: data.ok ? "Create a change branch, then discover devices or create desired state." : "Review Git command output and configuration.",
    });
  } catch (error) {
    failOutcome("Git setup failed.", error);
  }
}

function suggestedChangeBranch() {
  const slug = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const payload = changePayload();
  const site = slug(payload.site) || "site";
  const type = slug(appState.selectedChangeType || "change") || "change";
  return `change/${site}-${type}`;
}

async function runBranchAction(name, base, intro) {
  startOutcome("Change branch", intro);
  try {
    const data = await postJson("/api/git/branch", { name, base });
    appState.gitBranches = {
      ok: true,
      available: true,
      current: data.current,
      branches: data.branches || [],
      message: data.message,
    };
    if (appState.git && data.current) appState.git.branch = data.current;
    renderAll();
    $("git-branch-outcome").textContent = data.message;
    setOutcome({
      state: data.ok ? "Passed" : "Review",
      status: data.ok ? "pass" : "warn",
      title: data.ok ? (data.action === "created" ? "Change branch created." : "Switched branch.") : "Branch step needs review.",
      summary: data.message,
      expected: "Work each network change on its own reviewable Git branch.",
      actual: (data.steps || []).map((step) => `${step.ok ? "OK" : "CHECK"}: ${step.command}${!step.ok && step.stderr ? ` - ${step.stderr}` : ""}`).join("\n") || data.message,
      artifact: data.branch || "No branch",
      device: "No device config was changed.",
      next: data.ok ? "Create desired state, then commit artifacts to this branch." : "Fix the branch name or connect Git first.",
    });
  } catch (error) {
    failOutcome("Branch step failed.", error);
  }
}

async function createChangeBranch() {
  const name = $("git-new-branch").value.trim() || suggestedChangeBranch();
  const base = $("git-base-branch").value.trim() || "";
  await runBranchAction(name, base, `Create ${name} so this change is reviewable on its own branch.`);
}

async function switchGitBranch() {
  const name = $("git-branch-select").value;
  if (!name) {
    $("git-branch-outcome").textContent = "Pick a branch to switch to.";
    return;
  }
  await runBranchAction(name, "", `Switch the workspace to branch ${name}.`);
}

async function commitArtifacts() {
  const message = $("commit-message").value.trim();
  startOutcome("Commit artifacts", "Commit intent, rendered config, validation, and reports to the change branch.");
  try {
    const data = await postJson("/api/git/commit", { message, change_id: appState.activeChangeId });
    appState.lastCommit = data;
    appState.git = await getJson("/api/git/status");
    renderAll();
    setOutcome({
      state: data.ok ? "Passed" : "Review",
      status: data.ok ? "pass" : "warn",
      title: data.ok
        ? data.action === "nothing_to_commit"
          ? "Everything is already committed."
          : `Committed ${data.commit}.`
        : "Commit needs review.",
      summary: data.message,
      expected: "A reviewable commit containing every artifact of this change.",
      actual: (data.steps || []).map((step) => `${step.ok ? "OK" : "CHECK"}: ${step.command}`).join("\n") || data.message,
      artifact: data.commit || "no commit",
      device: "No device config was changed.",
      next: data.ok ? "Push the branch for review." : "Connect Git and create the change branch first.",
    });
  } catch (error) {
    failOutcome("Commit failed.", error);
  }
}

function renderPrSummary(push) {
  const pr = appState.gitPlan?.pull_request || {};
  if (!push?.ok) {
    $("pr-summary").textContent = push?.message || "Commit and push to produce a review-ready summary.";
    return;
  }
  $("pr-summary").textContent = [
    `Branch pushed: ${push.branch}`,
    `PR title: ${pr.title || "Network change"}`,
    `Body sections: ${(pr.body_sections || []).join(", ") || "n/a"}`,
    `Required review evidence: ${(pr.required_review_evidence || []).join(", ") || "n/a"}`,
  ].join("\n");
}

async function pushBranch() {
  startOutcome("Push for review", "Push the change branch to origin so the team can review before merge.");
  try {
    const data = await postJson("/api/git/push", { change_id: appState.activeChangeId });
    appState.lastPush = data;
    appState.git = await getJson("/api/git/status");
    if (data.ok && appState.plan?.intent_path) {
      appState.gitPlan = await postJson("/api/gitops/plan", {
        intent_path: appState.plan.intent_path,
        device_id: selectedDeviceId(),
        change_id: appState.activeChangeId || null,
      });
    }
    renderPrSummary(data);
    renderAll();
    setOutcome({
      state: data.ok ? "Passed" : "Review",
      status: data.ok ? "pass" : "warn",
      title: data.ok ? `Pushed ${data.branch} for review.` : "Push needs attention.",
      summary: data.message,
      expected: "The change branch reaches origin so it can be reviewed and merged.",
      actual: (data.steps || []).map((step) => `${step.ok ? "OK" : "CHECK"}: ${step.command}${!step.ok && step.stderr ? ` - ${step.stderr}` : ""}`).join("\n") || data.message,
      artifact: data.branch || "no branch",
      device: "No device config was changed.",
      next: data.ok ? "Open the change record in Evidence, or merge after review." : "Push from an authenticated terminal, or fix the remote in Setup.",
    });
  } catch (error) {
    failOutcome("Push failed.", error);
  }
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

function setStory(id, status, label) {
  const card = $(id);
  if (!card) return;
  card.className = `story-card ${status}`;
  card.querySelector("strong").textContent = label;
}

function setupGates() {
  const gitPass = Boolean(appState.git?.available);
  const sotPass = Boolean(appState.source?.ok && (appState.source.summary?.device_count || 0) > 0);
  const readPass = Boolean(appState.rezHealth?.ok);
  const labPass = Boolean(appState.health?.lab?.ok);
  return { gitPass, sotPass, readPass, labPass, passed: [gitPass, sotPass, readPass, labPass].filter(Boolean).length };
}

const STORY3_STEPS = [
  { id: "declare", label: "Declare" },
  { id: "branch", label: "Branch" },
  { id: "plan", label: "Plan" },
  { id: "validate", label: "Validate" },
  { id: "dryrun", label: "Dry-run" },
  { id: "commit", label: "Commit" },
  { id: "apply", label: "Apply" },
  { id: "verify", label: "Verify" },
  { id: "push", label: "Push" },
];

function story3Status() {
  const plan = appState.plan;
  const suggested = plan?.plan?.suggested_branch || "";
  const current = appState.gitBranches?.current || "";
  const onChangeBranch = Boolean(current && (current === suggested || current.startsWith("change/")));
  const done = {
    declare: Boolean(plan),
    branch: onChangeBranch,
    plan: Boolean(plan),
    validate: Boolean(plan?.ok),
    dryrun: Boolean(appState.dryRun?.ok),
    commit: Boolean(appState.lastCommit?.ok),
    apply: Boolean(appState.apply?.ok),
    verify: Boolean(appState.verify?.ok || appState.apply?.ok),
    push: Boolean(appState.lastPush?.ok),
  };
  let nextIndex = STORY3_STEPS.findIndex((step) => !done[step.id]);
  if (nextIndex === -1) nextIndex = STORY3_STEPS.length;
  return { done, nextIndex };
}

function renderStoryRail() {
  const rails = $$("[data-story-rail]");
  if (!rails.length) return;
  const { done, nextIndex } = story3Status();
  const html = STORY3_STEPS.map((step, index) => {
    const state = done[step.id] ? "done" : index === nextIndex ? "next" : "todo";
    return `<span class="story-step ${state}"><em>${index + 1}</em>${escapeHtml(step.label)}</span>`;
  }).join("");
  rails.forEach((rail) => {
    rail.innerHTML = html;
  });
}

function renderUserStories() {
  const gates = setupGates();
  setStory("story-ready", gates.passed === 4 ? "pass" : "warn", gates.passed === 4 ? "Ready" : `${gates.passed}/4 gates ready`);
  setStory(
    "story-discovery",
    appState.discovery?.ok ? "pass" : gates.readPass ? "warn" : "fail",
    appState.discovery?.ok ? "Device discovered" : gates.readPass ? "Ready to discover" : "Read access needed"
  );
  const { done, nextIndex } = story3Status();
  const doneCount = STORY3_STEPS.filter((step) => done[step.id]).length;
  const complete = doneCount >= STORY3_STEPS.length;
  const nextLabel = nextIndex < STORY3_STEPS.length ? `next: ${STORY3_STEPS[nextIndex].label.toLowerCase()}` : "complete";
  setStory(
    "story-change",
    appState.plan && appState.plan.ok === false ? "fail" : complete ? "pass" : doneCount ? "warn" : "warn",
    appState.plan && appState.plan.ok === false ? "Plan blocked" : doneCount ? (complete ? "Complete" : `Step ${doneCount}/9 · ${nextLabel}`) : "Not started"
  );
  const changeCount = appState.audit?.changes?.length || 0;
  setStory("story-audit", changeCount ? "pass" : "warn", changeCount ? `${changeCount} change record${changeCount === 1 ? "" : "s"}` : "No changes yet");
  setStory(
    "story-drift",
    appState.drift ? (appState.drift?.drift?.ok === false ? "fail" : "pass") : "warn",
    appState.drift ? (appState.drift?.drift?.ok === false ? "Drift found — reconcile" : "In sync") : "Not checked"
  );
}

function adapterSummary() {
  const platforms = appState.rezPlatforms?.platforms || [];
  if (!platforms.length) return "No Rez platforms loaded.";
  const names = platforms.map((item) => item.platform).slice(0, 7).join(", ");
  const extra = platforms.length > 7 ? `, +${platforms.length - 7} more` : "";
  return `${platforms.length} read/discovery adapters loaded: ${names}${extra}.`;
}

function labRunningCount() {
  const lab = appState.health?.lab || {};
  if (typeof lab.running_nodes === "number") return lab.running_nodes;
  return (String(lab.stdout || "").match(/\brunning\b/g) || []).length;
}

function labSummary() {
  const lab = appState.health?.lab || {};
  if (!lab.ok) return lab.message || "Lab not reachable from this runtime.";
  const running = labRunningCount();
  return running ? `${running} containerlab nodes are running.` : lab.message || "Containerlab is reachable.";
}

function renderHome() {
  $("sidebar-workspace").textContent = appState.gitBranches?.current || appState.git?.branch || "main";
  $("sidebar-lab").textContent = appState.health?.lab?.ok ? "Arista lab reachable" : "Lab not reachable from this runtime";
  renderUserStories();
}

function chipRow(element, items, empty = "Unavailable") {
  if (!items.length) {
    element.innerHTML = `<span class="chip muted">${escapeHtml(empty)}</span>`;
    return;
  }
  element.innerHTML = items.map((item) => `<span class="chip${item.tone ? ` ${item.tone}` : ""}">${escapeHtml(item.text)}</span>`).join("");
}

function setGateCard(cardId, pass, copyId, copyText) {
  const card = $(cardId);
  if (card) card.className = `setup-item gate ${pass ? "pass" : "warn"}`;
  const copy = $(copyId);
  if (copy) copy.textContent = copyText;
}

function renderSetup() {
  const gates = setupGates();
  const git = appState.git || {};
  const gitConfig = getPath(appState.uiConfig || {}, "git", {});
  setGateCard(
    "gate-git-card",
    gates.gitPass,
    "setup-git-copy",
    gates.gitPass
      ? `Connected. Base branch ${git.branch || "main"}, remote ${git.remote || "not set"}.`
      : "Not connected. Enter the repo URL and connect once — every change needs a reviewable home."
  );
  const repoInput = $("git-repo-url");
  if (document.activeElement !== repoInput && !repoInput.value) repoInput.value = git.remote || gitConfig.repo_url || "";
  const baseInput = $("git-base-branch");
  if (document.activeElement !== baseInput && !baseInput.value) baseInput.value = gitConfig.branch || git.branch || "main";
  $("connect-git").textContent = gates.gitPass ? "Update Git connection" : "Connect Git repo";

  const source = appState.source || {};
  setGateCard(
    "gate-sot-card",
    gates.sotPass,
    "setup-sot-copy",
    gates.sotPass
      ? `${source.summary?.device_count || 0} devices are trusted. Targeting and policy checks use this inventory.`
      : "No trusted devices yet. Discover and import at least one device before making changes."
  );
  $("setup-sot-detail").textContent = `Active provider: ${source.provider || "local_yaml"}. Lab credentials come from inventory defaults (lab only) and are never copied into imported records.`;

  const rezPlatformCount = (appState.rezPlatforms?.platforms || []).length;
  setGateCard(
    "gate-read-card",
    gates.readPass,
    "setup-rez-copy",
    gates.readPass
      ? `Read drivers are loaded (${rezPlatformCount} vendors). Pick the vendor when you discover a device.`
      : `Read drivers are unavailable: ${appState.rezHealth?.error || "unknown error"}.`
  );
  $("setup-rez-detail").textContent = "Used by discovery, verification, and drift. Read-only — never pushes config.";

  const lab = appState.health?.lab || {};
  setGateCard(
    "gate-lab-card",
    gates.labPass,
    "setup-lab-copy",
    gates.labPass
      ? "Arista lab is reachable. Every change is proven here before it counts."
      : "Lab is not reachable from this runtime. Open the ORB URL for dry-run and apply."
  );
  $("setup-lab-detail").textContent = "Runner: this runtime. Your browser never touches devices directly.";

  const startChange = $("start-change");
  if (startChange) startChange.disabled = gates.passed !== 4;
  renderConfigPanel();
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
            <strong>${escapeHtml(device.id)}</strong>
            <p>${escapeHtml(device.platform)} at ${escapeHtml(device.host)}:${escapeHtml(device.port)}</p>
          </div>
          <span>${escapeHtml(device.site || "unassigned")}</span>
        </article>
      `
    )
    .join("");
}

function renderDesiredSummary() {
  const payload = changePayload();
  const catalog = catalogItem();
  $("desired-title").textContent = catalog.label;
  $("desired-summary").textContent = `${catalog.outcome} Target: ${selectedDeviceId()} at ${payload.site}.`;
  $("desired-capability").innerHTML = [
    `<span>${escapeHtml(catalog.risk || "Review required")}</span>`,
    `<span>${catalog.lab_write_supported ? "Lab write supported" : "Plan only"}</span>`,
    `<span>${catalog.production_write_supported ? "Production ready" : "Production locked"}</span>`,
  ].join("");
  if (appState.plan?.pipeline?.intent_yaml) {
    $("desired-yaml").textContent = appState.plan.pipeline.intent_yaml;
  }

  const git = appState.git || {};
  const branches = appState.gitBranches?.branches || [];
  const current = appState.gitBranches?.current || git.branch || "";
  const suggested = appState.plan?.plan?.suggested_branch || suggestedChangeBranch();
  $("git-current-branch").textContent = git.available
    ? current
      ? current.startsWith("change/")
        ? current
        : `${current} (base — create a change branch)`
      : "detached"
    : "connect Git first (Setup)";
  const newBranch = $("git-new-branch");
  newBranch.placeholder = suggested;
  if (document.activeElement !== newBranch && !newBranch.value && appState.plan) newBranch.value = suggested;
  $("git-branch-select").innerHTML = ['<option value="">Select branch</option>']
    .concat(branches.map((name) => `<option value="${escapeHtml(name)}"${name === current ? " selected" : ""}>${escapeHtml(name)}</option>`))
    .join("");
  $("create-branch").disabled = !git.available;
  $("switch-branch").disabled = !git.available || !branches.length;
}

function renderPlan() {
  const plan = appState.plan;
  if (!plan) {
    $("plan-action").textContent = "No plan yet";
    $("plan-device").textContent = "-";
    $("plan-risk").textContent = "Unknown";
    $("plan-writes").textContent = "None";
    $("plan-summary-text").textContent = "Create desired state first.";
    $("plan-commands").textContent = "No commands generated yet.";
    $("plan-blast").innerHTML = "";
    $("plan-rollback").textContent = "No rollback plan yet.";
    $("rollback-confidence").textContent = "";
    $("plan-checks").innerHTML = "Create a plan to see the checks.";
    return;
  }
  const meta = plan.plan || {};
  const pipeline = plan.pipeline;
  const commands = commandListFromText(pipeline.render.config);
  $("plan-action").textContent = meta.title || meta.label || plan.pipeline.intent.change_type;
  $("plan-device").textContent = meta.target_device_id || selectedDeviceId();
  $("plan-risk").textContent = meta.risk || (pipeline.validation.status === "pass" ? "Review" : "Blocked");
  $("plan-writes").textContent = meta.lab_write_supported ? "Lab only" : "Locked";
  $("plan-summary-text").textContent = [
    "Netcode plan",
    "",
    `+ ${meta.title || meta.label || "desired state"}`,
    `  type: ${pipeline.intent.change_type}`,
    `  target: ${meta.target_device_id || selectedDeviceId()}`,
    `  site: ${pipeline.intent.site}`,
    `  risk: ${meta.risk || "review required"}`,
    "",
    "Device writes during plan: none",
    meta.lab_write_supported
      ? "Next: review validation, then dry-run in an Arista EOS config session."
      : "Next: review validation and evidence. Apply is locked for this intent type.",
  ].join("\n");
  $("plan-commands").textContent = commandListBlock(commands, "No device commands. This intent is source-of-truth only.");
  $("desired-yaml").textContent = pipeline.intent_yaml;

  const blast = meta.blast_radius || {};
  chipRow(
    $("plan-blast"),
    [
      { text: `Blast radius: ${blast.device_count || 0} device${(blast.device_count || 0) === 1 ? "" : "s"}`, tone: "warn" },
      ...(blast.devices || []).map((device) => ({ text: device })),
      ...(blast.objects || []).map((object) => ({ text: object, tone: "good" })),
    ],
    "No blast radius data"
  );
  const rollback = meta.rollback || {};
  $("plan-rollback").textContent = rollback.commands
    ? commandListBlock(commandListFromText(rollback.commands))
    : "No device rollback for this change type.";
  $("rollback-confidence").textContent = rollback.confidence
    ? `${rollback.confidence.level} confidence — ${rollback.confidence.reason}`
    : "";
  const checks = meta.checks || {};
  const checkItem = (check, phase) =>
    `<article class="check-item ${check.executable ? "pass" : "warn"}"><strong>${phase}: ${escapeHtml(check.description)}</strong><p>${
      check.executable ? "Runs live during the lab flow." : escapeHtml(check.note || "Definition only — execution not wired yet.")
    }</p></article>`;
  $("plan-checks").innerHTML =
    [...(checks.pre || []).map((check) => checkItem(check, "Pre")), ...(checks.post || []).map((check) => checkItem(check, "Post"))].join("") ||
    '<article class="check-item"><strong>No checks defined for this change type.</strong></article>';
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
          <article class="check-item ${escapeHtml(check.status)}">
            <strong>${escapeHtml(check.status.toUpperCase())}: ${escapeHtml(check.title)}</strong>
            <p>${escapeHtml(check.message)}</p>
          </article>
        `
      )
      .join("");
  }
  $("git-plan").textContent = appState.gitPlan ? formatJson(appState.gitPlan) : "Create a plan first.";
  const labSupported = Boolean(appState.plan?.plan?.lab_write_supported);
  $("dryrun-proof").textContent = appState.dryRun
    ? formatJson(appState.dryRun)
    : labSupported
      ? "Run dry-run after validation is reviewed."
      : "Dry-run is locked for this intent type. Review plan, validation, and audit evidence.";
  $("run-dry-run").disabled = !(appState.plan?.ok && labSupported);
}

function setGate(id, state, label) {
  const gate = $(id);
  gate.className = state;
  gate.querySelector("strong").textContent = label;
}

function renderApply() {
  const labSupported = Boolean(appState.plan?.plan?.lab_write_supported);
  setGate("gate-plan", appState.plan ? "pass" : "warn", appState.plan ? "Planned" : "Waiting");
  setGate("gate-validation", appState.plan?.ok ? "pass" : appState.plan ? "fail" : "warn", appState.plan?.ok ? "Passed" : appState.plan ? "Blocked" : "Waiting");
  setGate("gate-dryrun", !labSupported && appState.plan ? "warn" : appState.dryRun?.ok ? "pass" : appState.dryRun ? "fail" : "warn", !labSupported && appState.plan ? "Locked" : appState.dryRun?.ok ? "Passed" : appState.dryRun ? "Failed" : "Waiting");
  if (appState.rollback?.ok && !appState.changeLive) {
    setGate("gate-verify", "pass", "Rolled back");
  } else {
    setGate("gate-verify", appState.verify?.ok || appState.apply?.ok ? "pass" : appState.verify ? "fail" : "warn", appState.verify?.ok || appState.apply?.ok ? "Verified" : appState.verify ? "Failed" : "Waiting");
  }
  $("apply-change").disabled = !(appState.plan?.ok && labSupported && appState.dryRun?.ok);
  $("verify-change").disabled = !(appState.apply?.ok && appState.changeLive);
  $("rollback-change").disabled = !(appState.apply?.ok && appState.changeLive);
  $("commit-artifacts").disabled = !(appState.plan && appState.git?.available);
  $("push-branch").disabled = !(appState.lastCommit?.ok || (appState.git?.ahead || 0) > 0);
  const commitMessage = $("commit-message");
  if (document.activeElement !== commitMessage && !commitMessage.value && appState.plan?.plan?.slug) {
    commitMessage.value = `Netcode change ${appState.plan.plan.slug}`;
  }
  if (!labSupported && appState.plan) {
    $("apply-transcript").textContent = "Apply is locked for this intent type in the current MVP. Plan, validation, Git evidence, and audit records are still available.";
  } else if (appState.rollback) {
    $("apply-transcript").textContent = transcriptText(appState.rollback, formatJson(appState.rollback));
  } else if (appState.apply) {
    $("apply-transcript").textContent = transcriptText(appState.apply, formatJson(appState.apply));
  } else if (appState.dryRun) {
    $("apply-transcript").textContent = transcriptText(appState.dryRun, formatJson(appState.dryRun));
  } else {
    $("apply-transcript").textContent = "No device command has been committed.";
  }
}

function renderDrift() {
  $("drift-compliance").textContent = appState.drift?.compliance?.ok === false ? "Review" : appState.drift ? "Loaded" : "Unknown";
  $("drift-intent").textContent = appState.plan?.plan?.title || "None";
  $("drift-live").textContent = appState.drift?.live_state?.ok ? "Collected" : appState.drift ? "Unavailable" : "Not collected";
  $("drift-action").textContent = appState.drift?.drift?.ok === false ? "Reconcile" : "Review";
  $("drift-output").textContent = appState.drift ? formatJson(appState.drift) : "Create a plan, then check drift for that intent.";
}

function renderAll() {
  applyConfigToOperationalForms();
  renderChangeTypeGrid();
  renderDynamicFields();
  renderHome();
  renderSetup();
  renderInventory();
  renderDesiredSummary();
  renderPlan();
  renderValidation();
  renderApply();
  renderDrift();
  renderEvidence();
  renderStoryRail();
}

async function checkWorkspace({ silent = false } = {}) {
  if (!silent) startOutcome("Check workspace", "Load Git, source of truth, Rez adapter, desired-state catalog, lab, jobs, and audit status.");
  try {
    const [uiConfig, health, git, gitBranches, source, rezHealth, rezPlatforms, catalog, jobs, audit] = await Promise.all([
      getJson("/api/config/ui"),
      getJson("/api/health"),
      getJson("/api/git/status"),
      getJson("/api/git/branches"),
      getJson("/api/source-of-truth"),
      getJson("/api/adapters/rez/health"),
      getJson("/api/adapters/rez/platforms"),
      getJson("/api/desired-state/catalog"),
      getJson("/api/jobs"),
      getJson("/api/audit/sessions"),
    ]);
    appState.uiConfig = uiConfig.config;
    appState.uiConfigPath = uiConfig.path;
    appState.configHistory = uiConfig.history || [];
    appState.health = health;
    appState.git = git;
    appState.gitBranches = gitBranches;
    appState.source = source;
    appState.rezHealth = rezHealth;
    appState.rezPlatforms = rezPlatforms;
    appState.catalog = catalog;
    appState.jobs = jobs;
    appState.audit = audit;
    appState.configApplied = false;
    applyConfigToOperationalForms({ force: true });
    setRunState("Workspace checked", health.lab?.ok ? "pass" : "warn");
    renderAll();
    if (!silent) {
      setOutcome({
        state: health.lab?.ok ? "Passed" : "Review",
        status: health.lab?.ok ? "pass" : "warn",
        title: "Workspace check complete.",
        summary: "Configuration, Git, source of truth, Rez adapters, desired-state catalog, lab status, and audit records were loaded.",
        expected: "Confirm the platform is ready before making a network change.",
        actual: `${git.available ? "Git ready" : "Git needs setup"}. ${source.summary?.device_count || 0} devices. ${catalog.change_types?.length || 0} desired-state types. ${audit.sessions?.length || 0} command sessions logged.`,
        artifact: appState.uiConfigPath,
        device: "No device config was changed.",
        next: "Discover devices or create desired state.",
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
    renderUserStories();
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
  startOutcome("Create plan", "Create YAML intent, render candidate config, run static policy checks, and set apply gates. No device contact.");
  try {
    const payload = changePayload();
    const data = await postJson("/api/desired-state/plan", payload);
    appState.plan = data;
    appState.activeChangeId = data.change?.id || "";
    appState.lastCommit = null;
    appState.lastPush = null;
    appState.dryRun = null;
    appState.apply = null;
    appState.verify = null;
    appState.rollback = null;
    appState.changeLive = false;
    appState.drift = null;
    if (data.intent_path) {
      appState.gitPlan = await postJson("/api/gitops/plan", {
        intent_path: data.intent_path,
        device_id: selectedDeviceId(),
        change_id: data.change?.id || null,
      });
    }
    appState.audit = await getJson("/api/audit/sessions");
    renderAll();
    setRunState(data.ok ? "Planned" : "Blocked", data.ok ? "pass" : "fail");
    setView("plan");
    setOutcome({
      state: data.ok ? "Passed" : "Failed",
      status: data.ok ? "pass" : "fail",
      title: data.ok ? "Plan created." : "Plan blocked by validation.",
      summary: "The platform created typed desired-state YAML and rendered candidate config.",
      expected: "Generate a reviewable plan without touching the device.",
      actual: `${data.pipeline.validation.checks.length} checks returned ${data.pipeline.validation.status}. Apply gate: ${data.plan.lab_write_supported ? "lab supported" : "locked"}.`,
      artifact: data.intent_path,
      device: "No device config was changed.",
      next: data.ok ? "Review validation." : "Fix the request or policy issue before dry-run.",
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
  const labSupported = Boolean(appState.plan.plan?.lab_write_supported);
  setOutcome({
    state: appState.plan.ok ? "Passed" : "Failed",
    status: appState.plan.ok ? "pass" : "fail",
    title: "Validation reviewed.",
    summary: "Static checks are visible and the Git review plan is attached.",
    expected: "Inspect policy and generated config guardrails before any device contact.",
    actual: `${appState.plan.pipeline.validation.checks.length} validation checks reviewed. ${labSupported ? "Dry-run is available." : "Apply is locked for this intent type."}`,
    artifact: appState.plan.pipeline.artifacts?.report_markdown_path || "Validation report.",
    device: "No device config was changed.",
    next: appState.plan.ok && labSupported ? "Run lab dry-run." : "Review evidence or choose a lab-supported intent.",
  });
}

async function runDryRun() {
  if (!appState.plan?.intent_path) {
    failOutcome("Dry-run blocked.", new Error("Create a plan first."));
    return;
  }
  if (!appState.plan.plan?.lab_write_supported) {
    failOutcome("Dry-run locked.", new Error("This intent type is not enabled for lab device writes in the current MVP."), "Review plan and evidence.");
    return;
  }
  startOutcome("Run lab dry-run", "Open EOS config session, load candidate, collect diff, then abort. No commit.");
  try {
    const data = await postJson("/api/lab/dry-run", {
      intent_path: appState.plan.intent_path,
      device_id: selectedDeviceId(),
      change_id: appState.plan.change?.id || null,
    });
    appState.dryRun = data;
    appState.audit = await getJson("/api/audit/sessions");
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
  startOutcome("Apply in Arista lab", "Commit the validated candidate, verify intent state, and log the command session.");
  try {
    const data = await postJson("/api/lab/apply", {
      intent_path: appState.plan.intent_path,
      device_id: selectedDeviceId(),
      change_id: appState.plan.change?.id || null,
    });
    appState.apply = data;
    appState.changeLive = Boolean(data.ok);
    appState.rollback = null;
    appState.audit = await getJson("/api/audit/sessions");
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
      device: data.ok ? "Candidate config was committed in the Arista lab and logged." : "Commit did not complete safely.",
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
  startOutcome("Verify live state", "Collect read-only device evidence and prove the intent is present.");
  try {
    const data = await postJson("/api/verify/intent", {
      intent_path: appState.plan.intent_path,
      device_id: selectedDeviceId(),
      change_id: appState.plan.change?.id || null,
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
      artifact: `Read-only verification for ${data.change_type}.`,
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
  startOutcome("Rollback lab change", "Commit rollback config, verify the intent was removed, and log the session.");
  try {
    const data = await postJson("/api/lab/rollback", {
      intent_path: appState.plan.intent_path,
      device_id: selectedDeviceId(),
      change_id: appState.plan.change?.id || null,
    });
    appState.rollback = data;
    if (data.ok) appState.changeLive = false;
    appState.audit = await getJson("/api/audit/sessions");
    renderAll();
    setRunState(data.ok ? "Rolled back" : "Rollback failed", data.ok ? "pass" : "fail");
    setOutcome({
      state: data.ok ? "Passed" : "Failed",
      status: data.ok ? "pass" : "fail",
      title: data.ok ? "Rollback verified." : "Rollback failed.",
      summary: data.result?.message || "Rollback completed.",
      expected: "Remove the lab intent and prove it is absent.",
      actual: data.result?.message || "Rollback returned.",
      artifact: data.job ? `Job ${data.job.id}` : "Rollback result.",
      device: data.ok ? "Rollback config was committed in the Arista lab and logged." : "Rollback did not complete safely.",
      next: "Review evidence.",
    });
  } catch (error) {
    failOutcome("Rollback failed.", error);
  }
}

async function checkDrift() {
  startOutcome("Check drift", "Collect read-only state and compare the current desired state against the live device where supported.");
  try {
    const compliance = await getJson("/api/compliance/summary");
    const payload = { compliance };
    if (appState.plan?.intent_path) {
      if (appState.plan.pipeline.intent.change_type === "add_vlan") {
        payload.drift = await postJson("/api/drift/vlan", {
          intent_path: appState.plan.intent_path,
          device_id: selectedDeviceId(),
          change_id: appState.plan.change?.id || null,
        });
      } else {
        payload.live_state = await postJson("/api/adapters/rez/collect-state", { device_id: selectedDeviceId() });
        payload.note = "Deep drift comparison is currently wired for VLAN intents. Non-VLAN intents collect live state and stay audit-visible.";
      }
    } else {
      payload.note = "Create a plan first to compare a specific desired state.";
    }
    appState.drift = payload;
    renderDrift();
    setView("drift");
    setOutcome({
      state: "Passed",
      status: "pass",
      title: "Drift check complete.",
      summary: "Drift collection is read-only.",
      expected: "Compare trusted desired state against live network evidence.",
      actual: payload.drift?.message || payload.note || "Compliance summary loaded.",
      artifact: "Drift/compliance evidence loaded.",
      device: "No device config was changed.",
      next: "Review drift evidence or reconcile through a new desired-state plan.",
    });
  } catch (error) {
    failOutcome("Drift check failed.", error);
  }
}

async function refreshEvidence() {
  startOutcome("Refresh evidence", "Load latest jobs, workflow events, audit sessions, reports, and Git plan.");
  try {
    const [jobs, audit, config] = await Promise.all([getJson("/api/jobs"), getJson("/api/audit/sessions"), getJson("/api/config/ui")]);
    appState.jobs = jobs;
    appState.audit = audit;
    appState.uiConfig = config.config;
    appState.uiConfigPath = config.path;
    appState.configHistory = config.history || [];
    if (appState.plan?.change?.id) {
      appState.workflow = await getJson(`/api/workflow/change/${appState.plan.change.id}`);
      appState.gitPlan = await postJson("/api/gitops/plan", {
        intent_path: appState.plan.intent_path,
        device_id: selectedDeviceId(),
        change_id: appState.plan.change.id,
      });
    }
    renderEvidence();
    setOutcome({
      state: "Passed",
      status: "pass",
      title: "Evidence refreshed.",
      summary: "Latest jobs, workflow events, reports, command sessions, and Git review data are visible.",
      expected: "Collect audit evidence for the current flow.",
      actual: `${appState.jobs?.jobs?.length || 0} jobs and ${appState.audit?.sessions?.length || 0} command sessions loaded.`,
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
    selected_change_type: appState.selectedChangeType,
    current_change: appState.plan?.change || null,
    current_plan: appState.plan?.plan || null,
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
    audit_sessions: appState.audit?.sessions?.length || 0,
    ui_config: appState.uiConfigPath
      ? {
          path: appState.uiConfigPath,
          history_events: appState.configHistory.length,
          selected_change_type: getPath(appState.uiConfig || {}, "desired_state.selected_change_type", ""),
          workflow_count: changeTypeList().length,
        }
      : null,
  };
}

function populateRecordSelect() {
  const select = $("record-select");
  if (!select) return;
  const changes = appState.audit?.changes || [];
  const current = select.value || appState.activeChangeId;
  select.innerHTML = ['<option value="">Select a change</option>']
    .concat(
      changes.map((change) => {
        const label = `${String(change.id).slice(0, 8)} · ${String(change.intent_path || "").split("/").pop()} · ${change.workflow_state}`;
        return `<option value="${escapeHtml(String(change.id))}"${String(change.id) === String(current) ? " selected" : ""}>${escapeHtml(label)}</option>`;
      })
    )
    .join("");
}

async function loadChangeRecord(changeId) {
  if (!changeId) {
    appState.changeRecord = null;
    renderChangeRecord();
    return;
  }
  try {
    appState.changeRecord = await getJson(`/api/change/${changeId}/record`);
  } catch (error) {
    appState.changeRecord = { ok: false, error: String(error) };
  }
  renderChangeRecord();
}

function recordBlock(title, lines) {
  const body = lines.filter(Boolean).map((line) => `<p>${line}</p>`).join("");
  return `<article class="record-block"><h5>${escapeHtml(title)}</h5>${body || "<p>Not available.</p>"}</article>`;
}

function renderChangeRecord() {
  const container = $("change-record");
  if (!container) return;
  const record = appState.changeRecord;
  if (!record) {
    container.innerHTML = '<p class="setup-hint">Pick a change to see its full record.</p>';
    return;
  }
  if (!record.ok) {
    container.innerHTML = `<p class="setup-hint">Could not load the record: ${escapeHtml(record.error || "unknown error")}</p>`;
    return;
  }
  const esc = escapeHtml;
  const req = record.request || {};
  const plan = record.plan || {};
  const safety = record.safety || {};
  const failedChecks = (safety.checks || []).filter((check) => check.status !== "pass");
  const proofLine = (proof, label) =>
    proof?.present
      ? `${esc(label)}: ${esc(proof.status || "")} — ${esc(proof.message || "")} (${(proof.commands || []).length} device commands)`
      : `${esc(label)}: not run`;
  container.innerHTML = [
    recordBlock("Request", [
      `<strong>${esc(req.title || "")}</strong> (${esc(req.change_type || "")})`,
      `Site ${esc(req.site || "-")} · device ${esc(req.device_id || "-")} · requested by ${esc(req.requested_by || "-")}`,
      `Created ${esc(req.created_at || "-")} · state <strong>${esc(record.workflow_state || "-")}</strong>`,
    ]),
    `<article class="record-block"><h5>Plan</h5><p>Risk: ${esc(plan.risk || "-")} · Devices: ${esc((plan.blast_radius?.devices || []).join(", ") || "-")} · Objects: ${esc(
      (plan.blast_radius?.objects || []).join(", ") || "-"
    )}</p>${plan.commands ? `<pre class="mini-code">${esc(plan.commands.trim())}</pre>` : "<p>No device commands.</p>"}</article>`,
    recordBlock("Safety checks", [
      `Status: <strong>${esc(safety.status || "unknown")}</strong> (${(safety.checks || []).length} checks)`,
      failedChecks.length ? `Failed: ${failedChecks.map((check) => esc(`${check.id}: ${check.message}`)).join("; ")}` : "All checks passed.",
    ]),
    recordBlock("Lab proof", [proofLine(record.lab_proof, "Dry-run")]),
    recordBlock("Apply proof", [proofLine(record.apply_proof, "Apply")]),
    recordBlock("Verification", [
      record.verify_proof?.present
        ? `Verified with the apply job ${esc(record.verify_proof.job_id || "")} (${esc(record.verify_proof.status || "")}).`
        : "Not verified yet.",
    ]),
    `<article class="record-block"><h5>Rollback</h5><p>${
      record.rollback_record?.present
        ? proofLine(record.rollback_record, "Rollback executed")
        : `Planned before apply — ${esc(plan.rollback?.confidence?.level || "unknown")} confidence.`
    }</p>${plan.rollback?.commands ? `<pre class="mini-code">${esc(plan.rollback.commands.trim())}</pre>` : ""}</article>`,
    recordBlock("Git record", [
      `Branch ${esc(record.git?.branch || "-")}${record.git?.upstream ? ` → ${esc(record.git.upstream)}` : " (not pushed yet)"}${
        typeof record.git?.ahead === "number" ? ` · ${record.git.ahead} commit(s) ahead` : ""
      }`,
      (record.git?.actions || []).map((action) => esc(`${action.action}: ${action.message || ""}`)).join("<br />") ||
        "No git actions recorded for this change yet.",
    ]),
    recordBlock(
      "Artifact manifest",
      (record.manifest || []).map(
        (item) => `${item.exists ? "OK" : "MISSING"} · ${esc(item.artifact)} · <code>${esc(item.path)}</code>`
      )
    ),
  ].join("");
}

function renderEvidence() {
  if (!$("evidence-output")) return;
  populateRecordSelect();
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
    configState: appState.uiConfig ? formatJson({ path: appState.uiConfigPath, config: appState.uiConfig, history: appState.configHistory }) : "No UI configuration loaded yet.",
    audit: appState.audit ? formatJson(appState.audit) : "No audit data loaded yet.",
    jobs: appState.jobs ? formatJson(appState.jobs) : "No jobs loaded yet.",
  };
  $("evidence-output").textContent = outputs[artifact] || outputs.overview;
}

function clearChangeProofState() {
  appState.plan = null;
  appState.gitPlan = null;
  appState.dryRun = null;
  appState.apply = null;
  appState.verify = null;
  appState.rollback = null;
  appState.changeLive = false;
  appState.drift = null;
  setRunState("Draft");
}

function resetChangeProof() {
  clearChangeProofState();
  renderDesiredSummary();
  renderPlan();
  renderValidation();
  renderApply();
  renderDrift();
  renderEvidence();
}

function selectChangeType(changeType) {
  storeDynamicValues();
  appState.selectedChangeType = changeType;
  clearChangeProofState();
  renderChangeTypeGrid();
  renderDynamicFields();
  renderDesiredSummary();
  renderPlan();
  renderValidation();
  renderApply();
  renderDrift();
  renderEvidence();
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
  $("save-config").addEventListener("click", savePlatformConfig);
  $("reload-config").addEventListener("click", () => reloadPlatformConfig());
  $("reset-config").addEventListener("click", resetPlatformConfig);
  $("connect-git").addEventListener("click", connectGitRepo);
  $("create-branch").addEventListener("click", createChangeBranch);
  $("switch-branch").addEventListener("click", switchGitBranch);
  $("commit-artifacts").addEventListener("click", commitArtifacts);
  $("push-branch").addEventListener("click", pushBranch);
  $("record-select").addEventListener("change", (event) => loadChangeRecord(event.target.value));
  $$("#platform-config-form input, #platform-config-form select, #platform-config-form textarea").forEach((input) => input.addEventListener("input", syncQuickConfigToJson));
  $("discover-device").addEventListener("click", discoverDevice);
  $("save-discovered-device").addEventListener("click", saveDiscoveredDevice);
  $("create-plan").addEventListener("click", createPlan);
  $("review-validation").addEventListener("click", reviewValidation);
  $("run-dry-run").addEventListener("click", runDryRun);
  $("apply-change").addEventListener("click", applyChange);
  $("verify-change").addEventListener("click", verifyChange);
  $("rollback-change").addEventListener("click", rollbackChange);
  $("check-drift").addEventListener("click", checkDrift);
  $("refresh-evidence").addEventListener("click", refreshEvidence);
  $$("#change-form input, #change-form select, #change-form textarea").forEach((input) => input.addEventListener("input", resetChangeProof));
}

bindEvents();
checkWorkspace({ silent: true });
