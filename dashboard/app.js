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
  const active = tasks.filter((task) => ["QUEUED", "PLANNING", "READY", "RUNNING", "VERIFYING", "WAITING"].includes(task.status));
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
  renderIdle(health.runtime.idle || {});
  renderResources(data);
  renderChat(data.tasks);
  fillProjects(data.projects);
  if (state.selectedTask) renderDetail(state.selectedTask);
}

function renderIdle(idle) {
  const enabled = Boolean(idle.enabled);
  $("#idle-toggle").setAttribute("aria-pressed", String(enabled));
  $("#idle-status").textContent = enabled ? "ACTIVO" : "PAUSADO";
  $("#idle-status").className = enabled ? "status-on" : "status-off";
  const last = idle.last_supervision_result;
  $("#idle-detail").textContent = last === null || last === undefined ? "sin pasada" : "supervisión lista";
}

function renderResources(data) {
  const memoriesByKind = data.memories.reduce((counts, memory) => {
    counts[memory.kind] = (counts[memory.kind] || 0) + 1;
    return counts;
  }, {});
  const devices = (data.devices || []).map((device) => `<div class="resource-group"><div class="resource-title"><b>${esc(device.name)} · ${esc(device.platform || "generic")}</b><span class="resource-state state-${esc(device.status)}">${esc(device.status)}</span></div><small>${esc(device.description)} · transporte: ${esc(device.transport || "local")}</small><p>${device.capabilities.length ? device.capabilities.map(esc).join(" · ") : "Sin adaptador registrado"}</p></div>`).join("") || `<div class="empty">No hay dispositivos registrados.</div>`;
  const projects = data.projects.slice(0, 5).map((project) => `<div class="resource-row"><span>${esc(project.name)}</span><small>${project.enabled ? "habilitado" : "deshabilitado"}${project.codegraph ? " · grafo disponible" : ""}</small></div>`).join("") || `<div class="empty">No hay proyectos registrados.</div>`;
  const memories = Object.entries(memoriesByKind).map(([kind, count]) => `<div class="resource-row"><span>${esc(kind)}</span><strong>${count}</strong></div>`).join("") || `<div class="empty">No hay memoria persistida.</div>`;
  $("#resource-summary").innerHTML = `<div class="resource-section"><p class="eyebrow">DISPOSITIVOS</p>${devices}</div><div class="resource-section"><p class="eyebrow">PROYECTOS · ${data.projects.length}</p>${projects}</div><div class="resource-section"><p class="eyebrow">MEMORIA · ${data.memories.length}</p>${memories}</div>`;
}

function renderChat(tasks) {
  const chats = tasks.filter((task) => task.source === "DASHBOARD_CHAT" || task.metadata?.interaction === "chat").slice(0, 15).reverse();
  $("#chat-history").innerHTML = chats.map((task) => `<div class="chat-turn"><div class="chat-message user"><span>TÚ</span><p>${esc(task.goal)}</p></div><div class="chat-message assistant"><span>ASSISTANT · ${esc(task.status)}</span><p>${esc(task.result_summary || (task.status === "WAITING" ? "Necesita información adicional para continuar." : "Procesando en la cola persistente."))}</p></div></div>`).join("") || `<div class="empty">La conversación aparecerá aquí.</div>`;
}

function renderAnalytics(analytics) {
  const tokens = analytics.estimated_tokens || {};
  const actualTokens = analytics.actual_tokens || {};
  const latency = analytics.latency || {};
  const throughput = analytics.throughput || {};
  const model = analytics.model || "configured-provider";
  $("#analytics-note").textContent = `${model} · tokens estimados`;
  $("#analytics-kpis").innerHTML = [
    ["TOKENS TOTALES", formatNumber(tokens.total), "prompt + respuesta"],
    ["TOKENS MEDIDOS", actualTokens.available ? formatNumber(actualTokens.total) : "-", actualTokens.available ? "datos de Ollama" : "sin telemetría"],
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
  const order = ["RUNNING", "PLANNING", "QUEUED", "READY", "VERIFYING", "WAITING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"];
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
  const chatSelect = $("#chat-project");
  const chatCurrent = chatSelect.value;
  chatSelect.innerHTML = `<option value="">Sin proyecto</option>` + projects.filter((project) => project.enabled).map((project) => `<option value="${esc(project.id)}">${esc(project.name)}</option>`).join("");
  chatSelect.value = chatCurrent;
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
  const llmTrace = events.filter((event) => ["LLM_REQUEST", "LLM_RESPONSE"].includes(event.event_type));
  $("#detail-panel").classList.remove("hidden");
  $("#detail-title").textContent = task.goal;
  const controls = ["WAITING", "BLOCKED"].includes(task.status) ? `<button class="button primary" data-action="input">${task.status === "BLOCKED" ? "Resolver bloqueo" : "Aportar input"}</button>` : "";
  const resume = task.status === "WAITING" ? `<button class="button" data-action="resume">Reanudar</button>` : "";
  const cancel = ["SUCCEEDED", "FAILED", "CANCELLED"].includes(task.status) ? "" : `<button class="button" data-action="cancel">Cancelar</button>`;
  const nodeNames = Object.fromEntries(nodes.map((node) => [node.id, node.description]));
  const usageBlock = usage ? `<div class="usage-strip"><span>LLM <b>${usage.llm_calls}</b></span><span>TOOLS <b>${usage.tool_calls}</b></span><span>NODOS <b>${usage.nodes}</b></span><span>TOKENS <b>${formatNumber(usage.estimated_tokens)}</b></span><span>TIEMPO <b>${formatSeconds(usage.duration_seconds)}</b></span></div>` : "";
  const trace = llmTrace.map((event) => `<details class="trace-entry" ${event.event_type === "LLM_RESPONSE" ? "open" : ""}><summary>${esc(event.event_type)} · ${esc(event.payload?.role || "LLM")}</summary><pre>${esc(JSON.stringify(event.payload, null, 2))}</pre></details>`).join("") || "<div class=empty>Esta tarea aún no tiene trazas LLM.</div>";
  $("#detail-content").innerHTML = `<div class="detail-actions">${controls}${resume}${cancel}<span class="badge status-${esc(task.status)}">${esc(task.status)}</span></div>${usageBlock}<div class="llm-trace"><p class="eyebrow">LLM TRACE · REQUEST / RESPONSE</p>${trace}</div><div class="detail-grid"><div><p class="eyebrow">GRAPH NODES · ${nodes.length}</p><div class="node-list">${nodes.map((node) => { const item = nodeUsage[node.id] || {}; return `<div class="node-item status-${esc(node.status)}"><strong>${esc(node.description)}</strong><small>${esc(node.type)} · ${esc(node.status)} · retries ${node.retry_count}</small><small>${item.llm_calls || 0} LLM · ${item.tool_calls || 0} tools · ${formatNumber(item.estimated_tokens)} tokens est.</small>${node.error ? `<small>${esc(node.error)}</small>` : ""}</div>`; }).join("") || "<div class=empty>El grafo todavía no se ha generado.</div>"}</div><p class="eyebrow graph-label">DEPENDENCIES · ${edges.length}</p><div class="node-list">${edges.map((edge) => `<div class="node-item"><small>${esc(nodeNames[edge.from_node] || shortId(edge.from_node))} → ${esc(nodeNames[edge.to_node] || shortId(edge.to_node))} · ${esc(edge.dependency_type)}</small></div>`).join("") || "<div class=empty>Sin dependencias.</div>"}</div></div><div><p class="eyebrow">TASK EVENTS</p><div class="detail-events">${events.map((event) => `<div class="event"><i></i><div><b>${esc(event.event_type)}</b><span>${esc(event.payload?.reason || event.payload?.role || "evento persistido")}</span></div><time>${date(event.created_at)}</time></div>`).join("") || "<div class=empty>Sin eventos.</div>"}</div></div></div>`;
  $("#detail-content").querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => taskAction(button.dataset.action, task)));
}

function selectModule(module) {
  document.querySelectorAll(".module-panel, [data-module].module-panel, main > [data-module], main > div[data-module]").forEach((element) => {
    element.classList.toggle("module-hidden", element.dataset.module !== module);
  });
  document.querySelectorAll("[data-module-target]").forEach((button) => {
    button.classList.toggle("active", button.dataset.moduleTarget === module);
  });
  state.activeModule = module;
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
document.querySelectorAll("[data-module-target]").forEach((button) => button.addEventListener("click", () => selectModule(button.dataset.moduleTarget)));
$("#idle-toggle").addEventListener("click", async () => {
  try {
    const enabled = $("#idle-toggle").getAttribute("aria-pressed") !== "true";
    const idle = await api("/runtime/idle", { method: "PUT", body: JSON.stringify({ enabled }) });
    renderIdle(idle); toast(`Ciclo idle ${idle.enabled ? "activado" : "pausado"}`);
  } catch (error) { toast(`No se pudo cambiar idle: ${error.message}`, true); await load(); }
});
$("#reset-memory").addEventListener("click", async () => {
  if (!window.confirm("Esto borrará tareas, eventos, grafos, leases, resultados y memoria. Los proyectos se conservarán. ¿Continuar?")) return;
  try {
    const result = await api("/memory/reset", { method: "POST" });
    state.selectedTask = null;
    toast(`Estado restablecido: ${result.total} registros eliminados`);
    await load();
  } catch (error) { toast(`No se pudo reiniciar la memoria: ${error.message}`, true); }
});
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
$("#chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const task = await api("/chat", { method: "POST", body: JSON.stringify({ message: $("#chat-message").value, project_id: $("#chat-project").value || null }) });
    $("#chat-message").value = "";
    toast(`Consulta en cola: ${shortId(task.id)}`);
    await load();
  } catch (error) { toast(`No se pudo enviar el mensaje: ${error.message}`, true); }
});
selectModule("overview");
load();
window.setInterval(load, 5000);
