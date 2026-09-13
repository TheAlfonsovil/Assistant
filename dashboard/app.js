const state = { data: null, selectedTask: null };
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>\"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const date = (value) => value ? new Date(value).toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" }) : "-";
const shortId = (value) => value ? value.slice(0, 8) : "-";

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `HTTP ${response.status}`);
  }
  return response.json();
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.style.background = error ? "var(--red)" : "var(--cyan)";
  element.classList.add("show");
  window.setTimeout(() => element.classList.remove("show"), 3000);
}

async function load() {
  try {
    state.data = await api("/dashboard/data");
    render();
    $("#last-refresh").textContent = `Actualizado ${new Date().toLocaleTimeString("es-ES")}`;
  } catch (error) {
    toast(`No se pudo leer el runtime: ${error.message}`, true);
  }
}

function render() {
  const data = state.data;
  const health = data.health;
  const metrics = health.runtime.metrics || {};
  const tasks = data.tasks;
  const active = tasks.filter((task) => ["QUEUED", "PLANNING", "READY", "RUNNING", "VERIFYING", "WAITING", "BLOCKED"].includes(task.status));
  const successful = tasks.filter((task) => task.status === "SUCCEEDED").length;
  const failed = tasks.filter((task) => ["FAILED", "BLOCKED"].includes(task.status)).length;

  $("#hero-status").textContent = health.status || (health.llm_ready ? "READY" : "DEGRADED");
  $("#hero-health").className = `health-orb ${health.llm_ready ? "" : "warning"}`;
  $("#stats").innerHTML = [
    ["EN EJECUCIÓN", active.length, "Tareas activas"],
    ["COMPLETADAS", successful, "Resultado persistido"],
    ["INCIDENTES", failed, "Fallidas o bloqueadas"],
    ["NODOS", Object.values(data.node_status_counts).reduce((sum, value) => sum + value, 0), "En todos los grafos"],
  ].map(([label, value, note]) => `<div class="stat"><span>${label}</span><strong>${value}</strong><small class="muted">${note}</small></div>`).join("");

  renderTasks(tasks);
  renderAnalytics(data.analytics || {});
  renderChart(data.status_counts);
  renderMetrics(metrics, health.runtime);
  renderEvents(data.events);
  $("#context-summary").innerHTML = [
    ["Proyectos registrados", data.projects.length],
    ["Proyectos habilitados", data.projects.filter((project) => project.enabled).length],
    ["Memorias disponibles", data.memories.length],
    ["Recuperaciones al arrancar", health.recovered_nodes],
  ].map(([label, value]) => `<div class="context-item"><span>${label}</span><strong>${value}</strong></div>`).join("");
  fillProjects(data.projects);
  if (state.selectedTask) renderDetail(state.selectedTask);
}

function renderAnalytics(analytics) {
  const tokens = analytics.estimated_tokens || {};
  const latency = analytics.latency || {};
  const throughput = analytics.throughput || {};
  const model = analytics.model || "configured-provider";
  $("#analytics-note").textContent = `${model} · tokens estimados`;
  $("#analytics-kpis").innerHTML = [
    ["TOKENS TOTALES", formatNumber(tokens.total), "prompt + respuesta"],
    ["ÉXITO", `${throughput.success_rate ?? 0}%`, `${throughput.completed_tasks ?? 0} completadas`],
    ["DURACIÓN MEDIA", formatSeconds(latency.average_task_seconds), "por tarea terminada"],
    ["REINTENTOS", throughput.retries ?? 0, "recuperaciones programadas"],
  ].map(([label, value, note]) => `<div class="analytics-kpi"><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`).join("");

  const phases = analytics.phase_counts || {};
  $("#llm-analysis").innerHTML = Object.entries(phases).map(([phase, count]) => {
    const ratio = Math.min(100, count / Math.max(1, Object.values(phases).reduce((a, b) => a + b, 0)) * 100);
    return `<div class="analysis-row"><div><b>${esc(phase)}</b><small>${count} llamadas</small></div><div class="analysis-bar"><i style="width:${ratio}%"></i></div></div>`;
  }).join("") || `<div class="empty">Aún no hay llamadas LLM registradas.</div>`;
  $("#llm-analysis").insertAdjacentHTML("beforeend", `<div class="token-split"><span>Entrada <b>${formatNumber(tokens.prompt)}</b></span><span>Salida <b>${formatNumber(tokens.response)}</b></span></div>`);

  const tools = analytics.tool_counts || {};
  $("#tool-analysis").innerHTML = Object.entries(tools).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([tool, count]) => `<div class="analysis-row compact"><div><b>${esc(tool)}</b><small>${count} ejecuciones</small></div><strong>${formatSeconds(latency.average_tool_seconds)}</strong></div>`).join("") || `<div class="empty">Aún no hay herramientas ejecutadas.</div>`;
  const durations = analytics.task_durations || [];
  const maxDuration = Math.max(1, ...durations.map((item) => item.seconds));
  $("#speed-analysis").innerHTML = durations.slice(0, 8).map((item) => `<div class="speed-row"><div><b>${esc(item.goal)}</b><small>${formatSeconds(item.seconds)}</small></div><div class="analysis-bar"><i style="width:${Math.max(4, item.seconds / maxDuration * 100)}%"></i></div></div>`).join("") || `<div class="empty">Completa una tarea para ver velocidad.</div>`;
}

function formatNumber(value) { return new Intl.NumberFormat("es-ES").format(value || 0); }
function formatSeconds(value) { const seconds = Number(value || 0); return seconds < 60 ? `${seconds.toFixed(1)}s` : `${(seconds / 60).toFixed(1)}m`; }

function renderTasks(tasks) {
  const list = $("#task-list");
  const filter = ($("#task-filter")?.value || "").toLowerCase().trim();
  tasks = tasks.filter((task) => !filter || `${task.goal} ${task.status} ${task.id}`.toLowerCase().includes(filter));
  if (!tasks.length) {
    list.innerHTML = `<div class="empty">No hay tareas persistidas. Lanza una orden para empezar.</div>`;
    return;
  }
  list.innerHTML = tasks.slice(0, 25).map((task) => `<div class="task-row" data-task="${esc(task.id)}">
    <i class="task-accent status-${esc(task.status)}"></i><div><h3>${esc(task.goal)}</h3><span class="task-meta">${shortId(task.id)} · ${date(task.created_at)} · prioridad ${task.priority}</span></div><span class="badge status-${esc(task.status)}">${esc(task.status)}</span>
  </div>`).join("");
  list.querySelectorAll("[data-task]").forEach((row) => row.addEventListener("click", () => selectTask(row.dataset.task)));
}

function renderChart(counts) {
  const total = Object.values(counts).reduce((sum, value) => sum + value, 0) || 1;
  const order = ["RUNNING", "QUEUED", "READY", "WAITING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"];
  $("#status-chart").innerHTML = order.filter((status) => counts[status]).map((status) => `<div class="bar-line"><span>${status}</span><div class="bar"><i style="width:${Math.max(4, counts[status] / total * 100)}%"></i></div><b>${counts[status]}</b></div>`).join("") || `<div class="empty">Sin actividad todavía</div>`;
}

function renderMetrics(metrics, runtime) {
  const entries = [["Pasadas", metrics.passes], ["Despachos", metrics.tasks_dispatched], ["Errores de tarea", metrics.task_errors], ["Pasadas idle", metrics.idle_passes], ["Sin LLM", metrics.not_ready_passes], ["Errores runtime", metrics.runtime_errors]];
  $("#runtime-metrics").innerHTML = entries.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value ?? 0}</strong></div>`).join("");
  if (runtime.last_error) toast(`Runtime: ${runtime.last_error}`, true);
}

function renderEvents(events) {
  $("#event-stream").innerHTML = events.slice(0, 35).map((event) => `<div class="event"><i></i><div><b>${esc(event.event_type)}</b><span>${shortId(event.task_id)}${event.node_id ? ` · nodo ${shortId(event.node_id)}` : ""}</span></div><time>${date(event.created_at)}</time></div>`).join("") || `<div class="empty">El stream aparecerá cuando exista actividad.</div>`;
}

function fillProjects(projects) {
  const select = $("#project");
  const current = select.value;
  select.innerHTML = `<option value="">Resolver automáticamente</option>` + projects.filter((project) => project.enabled).map((project) => `<option value="${esc(project.id)}">${esc(project.name)}${project.is_default ? " · default" : ""}</option>`).join("");
  select.value = current;
}

function selectTask(taskId) {
  state.selectedTask = taskId;
  renderDetail(taskId);
  $("#detail-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderDetail(taskId) {
  const task = state.data.tasks.find((item) => item.id === taskId);
  if (!task) return;
  const nodes = state.data.task_nodes[taskId] || [];
  const edges = state.data.task_edges[taskId] || [];
  const events = state.data.events.filter((event) => event.task_id === taskId).slice(0, 40);
  const usage = (state.data.analytics?.task_usage || []).find((item) => item.id === taskId);
  const nodeUsage = state.data.analytics?.node_usage || {};
  $("#detail-panel").classList.remove("hidden");
  $("#detail-title").textContent = task.goal;
  const controls = ["WAITING"].includes(task.status) ? `<button class="button primary" data-action="input">Aportar input</button>` : "";
  const resume = task.status === "WAITING" ? `<button class="button" data-action="resume">Reanudar</button>` : "";
  const cancel = ["SUCCEEDED", "FAILED", "CANCELLED"].includes(task.status) ? "" : `<button class="button" data-action="cancel">Cancelar</button>`;
  const nodeNames = Object.fromEntries(nodes.map((node) => [node.id, node.description]));
  const usageBlock = usage ? `<div class="usage-strip"><span>LLM <b>${usage.llm_calls}</b></span><span>TOOLS <b>${usage.tool_calls}</b></span><span>NODOS <b>${usage.nodes}</b></span><span>TOKENS <b>${formatNumber(usage.estimated_tokens)}</b></span><span>TIEMPO <b>${formatSeconds(usage.duration_seconds)}</b></span></div>` : "";
  $("#detail-content").innerHTML = `<div class="detail-actions">${controls}${resume}${cancel}<span class="badge status-${esc(task.status)}">${esc(task.status)}</span></div>${usageBlock}<div class="detail-grid"><div><p class="eyebrow">GRAPH NODES · ${nodes.length}</p><div class="node-list">${nodes.map((node) => { const item = nodeUsage[node.id] || {}; return `<div class="node-item status-${esc(node.status)}"><strong>${esc(node.description)}</strong><small>${esc(node.type)} · ${esc(node.status)} · retries ${node.retry_count}</small><small>${item.llm_calls || 0} LLM · ${item.tool_calls || 0} tools · ${formatNumber(item.estimated_tokens)} tokens est.</small>${node.error ? `<small>${esc(node.error)}</small>` : ""}</div>`; }).join("") || "<div class=empty>El grafo todavía no se ha generado.</div>"}</div><p class="eyebrow graph-label">DEPENDENCIES · ${edges.length}</p><div class="node-list">${edges.map((edge) => `<div class="node-item"><small>${esc(nodeNames[edge.from_node] || shortId(edge.from_node))} → ${esc(nodeNames[edge.to_node] || shortId(edge.to_node))} · ${esc(edge.dependency_type)}</small></div>`).join("") || "<div class=empty>Sin dependencias.</div>"}</div></div><div><p class="eyebrow">TASK EVENTS</p><div class="detail-events">${events.map((event) => `<div class="event"><i></i><div><b>${esc(event.event_type)}</b><span>${esc(event.payload?.reason || event.payload?.role || "evento persistido")}</span></div><time>${date(event.created_at)}</time></div>`).join("") || "<div class=empty>Sin eventos.</div>"}</div></div></div>`;
  $("#detail-content").querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => taskAction(button.dataset.action, task)));
}

async function taskAction(action, task) {
  try {
    if (action === "cancel") await api(`/tasks/${task.id}/cancel`, { method: "POST" });
    if (action === "resume") await api(`/tasks/${task.id}/resume`, { method: "POST" });
    if (action === "input") {
      const answer = window.prompt("Input JSON para la tarea", "{}");
      if (answer === null) return;
      await api(`/tasks/${task.id}/input`, { method: "POST", body: JSON.stringify({ input: JSON.parse(answer) }) });
    }
    toast("Orden actualizada");
    await load();
  } catch (error) { toast(`No se pudo actualizar: ${error.message}`, true); }
}

$("#refresh").addEventListener("click", load);
$("#task-filter").addEventListener("input", () => state.data && renderTasks(state.data.tasks));
$("#new-task").addEventListener("click", () => $("#modal").classList.remove("hidden"));
$("#close-modal").addEventListener("click", () => $("#modal").classList.add("hidden"));
$("#close-detail").addEventListener("click", () => { state.selectedTask = null; $("#detail-panel").classList.add("hidden"); });
$("#task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const body = { goal: $("#goal").value, priority: Number($("#priority").value || 0) };
    if ($("#project").value) body.project_id = $("#project").value;
    const task = await api("/tasks", { method: "POST", body: JSON.stringify(body) });
    $("#modal").classList.add("hidden"); $("#goal").value = ""; toast(`Orden creada: ${shortId(task.id)}`); await load(); selectTask(task.id);
  } catch (error) { toast(`No se pudo crear: ${error.message}`, true); }
});
load();
window.setInterval(load, 5000);
