const $ = (id) => document.getElementById(id);
const BASE_PATH = document.querySelector('meta[name="toothless-base-path"]').content.replace(/\/$/, "");
let cy;
let selectedId = "";
let refreshTimer;

const palette = { project: "#be95ff", task: "#33b1ff", memory: "#42be65", file: "#ff832b", artifact: "#ff832b" };

function setAuthenticated(authenticated) {
  $("refresh").classList.toggle("hidden", !authenticated);
  $("logout").classList.toggle("hidden", !authenticated);
}

function setStatus(message, busy = false) {
  $("refresh-status").textContent = message;
  $("refresh-status").parentElement.querySelector("i").style.background = busy ? "#e1b873" : "#76d8a8";
}

async function api(path) {
  const response = await fetch(`${BASE_PATH}${path}`, { credentials: "same-origin", cache: "no-store" });
  if (response.status === 401) {
    setAuthenticated(false);
    $("explorer").classList.add("hidden");
    $("login").classList.remove("hidden");
    $("login-error").textContent = "This session expired or was revoked. Request a fresh one-time code in Telegram.";
    throw new Error("Authentication expired");
  }
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function initGraph() {
  cy = window.cytoscape({
    container: $("graph"),
    elements: [],
    minZoom: 0.25,
    maxZoom: 2.4,
    wheelSensitivity: 0.18,
    selectionType: "single",
    style: [
      { selector: "node", style: { "background-color": (e) => palette[e.data("type")] || "#a8a8a8", label: "data(label)", color: "#f4f4f4", "font-size": 11, "font-weight": 500, "text-wrap": "ellipsis", "text-max-width": 120, "text-valign": "bottom", "text-margin-y": 8, width: (e) => e.data("type") === "project" ? 38 : 28, height: (e) => e.data("type") === "project" ? 38 : 28, "border-width": (e) => e.data("state") === "superseded" ? 2 : 0, "border-color": "#8a3ffc", "border-style": "dashed", "overlay-opacity": 0 } },
      { selector: "node:selected", style: { "border-width": 3, "border-color": "#ffffff" } },
      { selector: "edge", style: { width: 1, "line-color": "#6f6f6f", "target-arrow-color": "#a8a8a8", "target-arrow-shape": "triangle", "curve-style": "bezier", label: "data(label)", color: "#c6c6c6", "font-size": 8, "text-rotation": "autorotate", "text-background-color": "#262626", "text-background-opacity": 1, "text-background-padding": 2, "arrow-scale": 0.7 } },
      { selector: "edge[type = 'MEMORY_SUPERSEDES_MEMORY']", style: { "line-style": "dashed", "line-color": "#a56eff", "target-arrow-color": "#a56eff" } },
    ],
    layout: { name: "cose", animate: false, fit: true, padding: 62, nodeRepulsion: 3600, idealEdgeLength: 90, gravity: 0.3, numIter: 180 },
  });
  cy.on("select", "node", async (event) => {
    selectedId = event.target.id();
    $("expand").disabled = false;
    await inspect(selectedId);
  });
  cy.on("unselect", "node", () => {
    if (cy.$("node:selected").length === 0) {
      selectedId = "";
      $("expand").disabled = true;
    }
  });
}

function graphElements(payload) {
  return [
    ...payload.nodes.map((node) => ({ data: { ...node, color: palette[node.type] || "#aaa" } })),
    ...payload.edges.map((edge) => ({ data: { ...edge, label: edge.type.replaceAll("_", " ") } })),
  ];
}

function mergeGraph(payload) {
  const elements = graphElements(payload);
  const incomingNodes = new Set(elements.filter((item) => !item.data.source).map((item) => item.data.id));
  const incomingEdges = new Set(elements.filter((item) => item.data.source).map((item) => item.data.id));
  const isInitial = cy.nodes().length === 0;
  cy.edges().forEach((edge) => { if (!incomingEdges.has(edge.id())) edge.remove(); });
  cy.nodes().forEach((node) => { if (!incomingNodes.has(node.id())) node.remove(); });
  const added = [];
  for (const item of elements) {
    const existing = cy.getElementById(item.data.id);
    if (existing.length) existing.data(item.data);
    else { cy.add(item); if (!item.data.source) added.push(item.data.id); }
  }
  if (isInitial) cy.layout({ name: "cose", animate: true, animationDuration: 260, fit: true, padding: 62, nodeRepulsion: 3600, idealEdgeLength: 90, gravity: 0.3, numIter: 180 }).run();
  else if (added.length) {
    const center = selectedId && cy.getElementById(selectedId).length ? cy.getElementById(selectedId).position() : { x: cy.width() / 2, y: cy.height() / 2 };
    added.forEach((id, index) => cy.getElementById(id).position({ x: center.x + Math.cos(index * 2.4) * (70 + index * 7), y: center.y + Math.sin(index * 2.4) * (70 + index * 7) }));
  }
  const count = document.createElement("span");
  count.textContent = String(cy.nodes().length);
  const label = document.createElement("span");
  label.textContent = "records";
  $("node-count").replaceChildren(count, label);
  $("source-count").textContent = `${payload.source_ids.length} provenance references`;
  $("empty").classList.toggle("hidden", cy.nodes().length > 0);
  renderRecordList();
}

function renderRecordList() {
  const list = $("record-list");
  list.replaceChildren();
  const title = document.createElement("div");
  title.className = "record-list-title";
  title.textContent = "Records in this view";
  list.append(title);
  cy.nodes().forEach((node) => {
    const button = document.createElement("button");
    button.className = "record-choice";
    button.textContent = `${node.data("type")} · ${node.data("label")}`;
    button.setAttribute("aria-current", node.id() === selectedId ? "true" : "false");
    button.addEventListener("click", () => selectNode(node.id()));
    list.append(button);
  });
}

async function refreshGraph() {
  setStatus("Refreshing…", true);
  try {
    const params = new URLSearchParams({ q: $("search").value.trim() || "mechanochemistry", type: $("type-filter").value, project: $("project-filter").value, depth: "2", limit: "48" });
    const payload = await api(`/api/graph?${params}`);
    mergeGraph(payload);
    setStatus(`Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
  } catch (error) {
    if (error.message !== "Authentication expired") setStatus("Refresh failed");
  }
}

function text(parent, value, className = "") {
  const node = document.createElement("div");
  if (className) node.className = className;
  node.textContent = value ?? "—";
  parent.append(node);
  return node;
}

function meta(parent, label, value) {
  const row = document.createElement("div"); row.className = "meta-row";
  text(row, label); text(row, value, "meta-value"); parent.append(row);
}

function section(parent, label) { text(parent, label, "section-label"); }

async function inspect(id) {
  try {
    const detail = await api(`/api/node?id=${encodeURIComponent(id)}`);
    const container = $("inspector-content");
    container.replaceChildren();
    const entity = detail.entity;
    text(container, entity.type, "type-tag");
    text(container, entity.label, "inspector-title");
    meta(container, "State", entity.state);
    meta(container, "Source", `${entity.source_type}:${entity.source_id}`);
    meta(container, "Created", entity.created_at);
    meta(container, "Updated", entity.updated_at);
    if (detail.description) { section(container, "Project description"); text(container, detail.description, "detail-copy"); }
    if (detail.content) { section(container, "Saved memory"); text(container, detail.content, "detail-copy"); }
    if (detail.task) {
      section(container, "Task record");
      meta(container, "Status", detail.task.status);
      meta(container, "Task ID", detail.task.task_id);
      meta(container, "Request", detail.task.request);
      if (detail.task.files?.length) { section(container, "Uploaded files"); detail.task.files.forEach((name) => text(container, name, "detail-copy")); }
      if (detail.task.artifacts?.length) { section(container, "Generated files"); detail.task.artifacts.forEach((name) => text(container, name, "detail-copy")); }
      if (detail.task.execution_ids?.length) { section(container, "Executions"); detail.task.execution_ids.forEach((value) => text(container, value, "detail-copy")); }
    }
    if (detail.history?.length) {
      section(container, "Memory history");
      detail.history.forEach((entry) => {
        const card = document.createElement("div"); card.className = "history-card";
        text(card, `${entry.state} · ${entry.kind} · ${entry.id}`, "history-state");
        text(card, entry.content, "detail-copy");
        text(card, `Source: ${entry.source_id} · ${entry.updated_at}`, "history-state");
        container.append(card);
      });
    }
    if (detail.references?.length) {
      section(container, "References");
      detail.references.forEach((item) => meta(container, item.type, `${item.label} · ${item.id}`));
    }
    if (detail.relationships?.length) {
      section(container, "Relationships");
      const list = document.createElement("div"); list.className = "link-list";
      detail.relationships.forEach((edge) => {
        const neighborId = edge.source === id ? edge.target : edge.source;
        const neighbor = detail.related.find((item) => item.id === neighborId);
        const button = document.createElement("button"); button.className = "link-item";
        button.textContent = `${edge.type.replaceAll("_", " ")} · ${neighbor?.label || neighborId}`;
        button.addEventListener("click", () => selectNode(neighborId));
        list.append(button);
      });
      container.append(list);
    }
    if (detail.source_ids?.length) { section(container, "Source records"); text(container, detail.source_ids.join("\n"), "detail-copy"); }
    container.classList.remove("hidden");
    $("inspector").querySelector(".inspector-empty").classList.add("hidden");
  } catch { setStatus("Could not inspect record"); }
}

function selectNode(id) {
  const node = cy.getElementById(id);
  if (!node.length) return;
  cy.$("node:selected").unselect();
  node.select();
  cy.animate({ center: { eles: node }, duration: 180 });
}

async function expandSelected() {
  if (!selectedId) return;
  try { mergeGraph(await api(`/api/expand?id=${encodeURIComponent(selectedId)}&limit=24`)); }
  catch { setStatus("Could not expand this record"); }
}

async function loadProjects() {
  try {
    const { projects } = await api("/api/projects");
    const select = $("project-filter");
    select.querySelectorAll("option:not(:first-child)").forEach((option) => option.remove());
    for (const project of projects) {
      const option = document.createElement("option"); option.value = project.id; option.textContent = project.name; select.append(option);
    }
  } catch { setStatus("Could not load projects"); }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = $("token").value.trim();
  $("token").value = "";
  try {
    const response = await fetch(`${BASE_PATH}/authorize`, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }), cache: "no-store",
    });
    if (!response.ok) throw new Error("The one-time code is invalid or expired.");
    setAuthenticated(true);
    $("login").classList.add("hidden"); $("explorer").classList.remove("hidden");
    initGraph(); await loadProjects(); await refreshGraph();
    clearInterval(refreshTimer); refreshTimer = setInterval(refreshGraph, 10000);
  } catch {
    $("login-error").textContent = "That one-time code did not work. Request a fresh code in Telegram.";
  }
});
$("search-button").addEventListener("click", refreshGraph);
$("search").addEventListener("keydown", (event) => { if (event.key === "Enter") refreshGraph(); });
$("project-filter").addEventListener("change", refreshGraph);
$("type-filter").addEventListener("change", refreshGraph);
$("refresh").addEventListener("click", refreshGraph);
window.addEventListener("focus", refreshGraph);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshGraph(); });
$("fit").addEventListener("click", () => cy?.fit(undefined, 54));
$("expand").addEventListener("click", expandSelected);

$("logout").addEventListener("click", async () => {
  await fetch(`${BASE_PATH}/logout`, { method: "POST", credentials: "same-origin", cache: "no-store" });
  clearInterval(refreshTimer);
  setAuthenticated(false);
  $("explorer").classList.add("hidden"); $("login").classList.remove("hidden");
});
