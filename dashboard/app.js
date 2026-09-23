const state = { data: null, selectedTask: null, taskDetails: {}, view: "overview" };
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const date = (value) => value ? new Date(value).toLocaleString("es-ES", { dateStyle: "short", timeStyle: "medium" }) : "-";
const shortId = (value) => value ? value.slice(0, 8) : "-";
const json = (value) => JSON.stringify(value ?? {}, null, 2);

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
  return response.json();
}
function toast(message, error = false) {
  const element = $("#toast"); element.textContent = message; element.className = `toast show ${error ? "error" : ""}`;
  window.setTimeout(() => element.classList.remove("show"), 3500);
}
function setView(view) {
  state.view = view;
  document.querySelectorAll("[data-view-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.viewPanel === view));
  document.querySelectorAll("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  const titles = { overview: "Resumen operativo", tasks: "Tareas persistentes", trace: "Observabilidad", activity: "Actividad reciente", resources: "Recursos conectados", devices: "Dispositivos", projects: "Proyectos", memory: "Memoria", metrics: "Métricas", chat: "Chat rápido" };
  $("#view-title").textContent = titles[view] || "Operations";
  if (view === "trace" && state.selectedTask) renderTrace(state.selectedTask);
}
function renderIdle(idle) {
  const enabled = Boolean(idle.enabled);
  $("#idle-toggle").setAttribute("aria-pressed", String(enabled));
  $("#idle-status").textContent = enabled ? "ACTIVO" : "PAUSADO";
}
function render(data) {
  state.data = data;
  const tasks = data.tasks || [], health = data.health, runtime = health.runtime || {}, metrics = runtime.metrics || {};
  const active = tasks.filter((task) => ["QUEUED", "PLANNING", "READY", "RUNNING", "VERIFYING", "FINALIZING", "WAITING"].includes(task.status));
  const failed = tasks.filter((task) => ["FAILED", "BLOCKED"].includes(task.status));
  $("#hero-status").textContent = health.status || "UNKNOWN";
  $("#runtime-label").textContent = health.llm_ready ? "READY" : "DEGRADED";
  $("#health-dot").className = health.llm_ready ? "ready" : "warning";
  $("#runtime-dot").className = health.llm_ready ? "ready" : "warning";
  const displayTokens = data.analytics?.display_tokens || data.analytics?.estimated_tokens || {};
  const tokenLabel = displayTokens.source === "ollama" ? "TOKENS REALES" : displayTokens.source === "ollama_partial" ? "TOKENS REALES*" : "TOKENS EST.";
  $("#stats").innerHTML = [["ACTIVAS", active.length], ["COMPLETADAS", tasks.filter((task) => task.status === "SUCCEEDED").length], ["INCIDENTES", failed.length], [tokenLabel, displayTokens.total ? formatNumber(displayTokens.total) : 0]].map(([label, value]) => `<div class="stat"><small>${label}</small><strong>${value}</strong></div>`).join("");
  renderMetrics(metrics); renderOperationalHealth(health, runtime); renderChart(data.status_counts || {}); renderAnalytics(data.analytics || {}); renderPerformanceMetrics(data); renderTasks(tasks); renderEvents(data.events || []); renderResources(data); renderCollections(data); renderChat(tasks); fillProjects(data.projects || []); renderIdle(runtime.idle || {});
  if (state.selectedTask) {
    renderInspector(state.selectedTask);
    renderTrace(state.selectedTask);
    loadTaskDetail(state.selectedTask);
  }
}
function renderMetrics(metrics) {
  const values = [["Pasadas", metrics.passes], ["Despachos", metrics.tasks_dispatched], ["Errores", metrics.task_errors], ["Pasadas idle", metrics.idle_passes], ["Sin LLM", metrics.not_ready_passes], ["Errores runtime", metrics.runtime_errors]];
  $("#runtime-metrics").innerHTML = values.map(([label, value]) => `<div class="metric"><small>${label}</small><b>${value ?? 0}</b></div>`).join("");
  $("#runtime-passes").textContent = formatNumber(metrics.passes);
  $("#runtime-dispatches").textContent = formatNumber(metrics.tasks_dispatched);
  $("#runtime-errors").textContent = formatNumber(metrics.task_errors + metrics.runtime_errors);
}
function renderOperationalHealth(health, runtime) {
  const database = health.database || {};
  const idle = runtime.idle || {};
  const rows = [
    ["API / worker", health.status || "UNKNOWN"],
    ["SQLite", database.status === "ok" ? `${database.journal_mode || "connected"} · ${database.integrity || "unknown"}` : "DEGRADED"],
    ["Mantenimiento", idle.last_maintenance_at ? date(idle.last_maintenance_at) : "pendiente"],
    ["Errores mantenimiento", idle.maintenance_errors || 0],
  ];
  $("#operational-health").innerHTML = rows.map(([label, value]) => `<span><small>${esc(label)}</small><b>${esc(value)}</b></span>`).join("");
}
function renderChart(counts) {
  const total = Math.max(1, Object.values(counts).reduce((sum, value) => sum + value, 0));
  const order = ["RUNNING", "PLANNING", "QUEUED", "READY", "VERIFYING", "FINALIZING", "WAITING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"];
  $("#status-chart").innerHTML = order.filter((key) => counts[key]).map((key) => `<div class="bar-line"><span>${key}</span><i><b style="width:${Math.max(5, counts[key] / total * 100)}%"></b></i><strong>${counts[key]}</strong></div>`).join("") || `<div class="empty">Sin tareas persistidas.</div>`;
}
function renderAnalytics(analytics) {
  const tokens = analytics.display_tokens || analytics.estimated_tokens || {}, throughput = analytics.throughput || {}, latency = analytics.latency || {};
  $("#analytics-note").textContent = analytics.model || "modelo configurado";
  const tokenLabel = tokens.source === "ollama" ? "TOKENS REALES" : tokens.source === "ollama_partial" ? "TOKENS REALES*" : "TOKENS EST.";
  const taskUsage = analytics.task_usage || [];
  $("#analytics-kpis").innerHTML = [[tokenLabel, formatNumber(tokens.total)], ["ÉXITO", `${throughput.success_rate ?? 0}%`], ["PREFILL MEDIO", formatSeconds(latency.prefill_seconds)], ["GENERACIÓN MEDIA", formatSeconds(latency.generation_seconds)], ["TOKENS/S", formatNumber(latency.generation_tokens_per_second)], ["REINTENTOS", throughput.retries ?? 0]].map(([label, value]) => `<div><small>${label}</small><b>${value}</b></div>`).join("");
  const phases = analytics.phase_counts || {};
  const phaseRows = Object.entries(phases).map(([phase, count]) => `<div class="analysis-row"><span>${esc(phase)}</span><b>${count} intercambios</b></div>`).join("");
  const taskRows = taskUsage.map((item) => `<div class="analysis-row"><span>${esc(item.goal || shortId(item.id))}</span><b>${formatNumber(item.actual_tokens_available ? item.actual_tokens : item.estimated_tokens)} tokens · prefill ${formatSeconds(item.prefill_seconds)} · gen ${formatSeconds(item.generation_seconds)} · reintentos ${item.retry_rate ?? 0}% (${item.retries || 0})</b>${Object.entries(item.phase_usage || {}).map(([role, phase]) => `<small>${esc(role)}: ${formatNumber((phase.prompt_tokens || 0) + (phase.response_tokens || phase.estimated_tokens || 0))} tokens · ${formatSeconds((phase.prefill_seconds || 0) + (phase.generation_seconds || 0))}</small>`).join("")}</div>`).join("");
  $("#llm-analysis").innerHTML = `<div class="timing-split"><div><small>PREFILL / entrada</small><i><b style="width:${timingRatio(latency.prefill_seconds, latency.prefill_seconds + latency.generation_seconds)}%"></b></i></div><div><small>GENERACIÓN / salida</small><i><b class="generation-bar" style="width:${timingRatio(latency.generation_seconds, latency.prefill_seconds + latency.generation_seconds)}%"></b></i></div></div>${taskRows || phaseRows || `<div class="empty">Aún no hay actividad LLM.</div>`}`;
}
function renderPerformanceMetrics(data) {
  const analytics = data.analytics || {}, tasks = analytics.task_usage || [], nodes = analytics.metrics_rows || [];
  const selected = $("#metrics-task-filter")?.value || "";
  const taskRows = tasks.filter((item) => !selected || item.id === selected);
  $("#metrics-task-filter").innerHTML = `<option value="">Todas las tareas</option>${(analytics.task_usage || []).map((item) => `<option value="${esc(item.id)}">${esc(item.goal || shortId(item.id))}</option>`).join("")}`;
  $("#metrics-task-filter").value = selected;
  const totalTokens = taskRows.reduce((sum, item) => sum + (item.actual_tokens_available ? item.actual_tokens : item.estimated_tokens || 0), 0);
  const totalRetries = taskRows.reduce((sum, item) => sum + (item.retries || 0), 0);
  $("#metrics-kpis").innerHTML = [["TAREAS", taskRows.length], ["DURACIÓN MEDIA", formatSeconds(taskRows.reduce((sum, item) => sum + (item.duration_seconds || 0), 0) / Math.max(1, taskRows.length))], ["TOKENS", formatNumber(totalTokens)], ["REINTENTOS", `${totalRetries} · ${taskRows.length ? (totalRetries / taskRows.length).toFixed(1) : 0} por tarea`]].map(([label, value]) => `<div class="stat"><small>${label}</small><strong>${value}</strong></div>`).join("");
  $("#task-metrics-table").innerHTML = taskRows.map((item) => `<div class="metrics-row"><strong>${esc(item.goal || shortId(item.id))}</strong><span>${esc(item.status)}</span><span>${formatSeconds(item.duration_seconds)}</span><span>${formatNumber(item.actual_tokens_available ? item.actual_tokens : item.estimated_tokens)} tokens</span><span>${item.llm_calls || 0} LLM · ${item.tool_calls || 0} tools</span><span>${item.retries || 0} reintentos (${item.retry_rate || 0}%)</span></div>`).join("") || '<div class="empty">Sin métricas de tareas.</div>';
  $("#node-metrics-table").innerHTML = nodes.filter((item) => !selected || item.task_id === selected).sort((a, b) => (b.duration_seconds || 0) - (a.duration_seconds || 0)).map((item) => `<div class="metrics-row"><strong>${esc(item.label || shortId(item.task_id))}</strong><span>${esc(item.type || "node")}</span><span>${formatSeconds(item.duration_seconds)}</span><span>${formatNumber(item.actual_tokens_available ? item.actual_tokens : item.estimated_tokens)} tokens</span><span>${item.llm_calls || 0} LLM · ${item.tool_calls || 0} tools</span><span>${item.retry_count || 0} reintentos</span></div>`).join("") || '<div class="empty">Sin métricas de nodos.</div>';
}
function renderTasks(tasks) {
  const filter = ($( "#task-filter")?.value || "").toLowerCase().trim();
  const statusFilter = $("#task-status-filter")?.value || "";
  const activeStatuses = ["QUEUED", "PLANNING", "READY", "RUNNING", "VERIFYING", "FINALIZING"];
  const filtered = tasks.filter((task) => (!statusFilter || (statusFilter === "active" ? activeStatuses.includes(task.status) : task.status === statusFilter)) && (!filter || `${task.goal} ${task.status} ${task.id}`.toLowerCase().includes(filter)));
  const active = tasks.filter((task) => activeStatuses.includes(task.status));
  const next = tasks.filter((task) => ["QUEUED", "READY"].includes(task.status)).sort((a, b) => (b.priority || 0) - (a.priority || 0) || new Date(a.created_at) - new Date(b.created_at))[0];
  $("#queue-summary").innerHTML = `<span><small>EN CURSO</small><b>${active.length}</b></span><span><small>SIGUIENTE</small><b>${next ? esc(next.goal) : "—"}</b></span><span><small>ACTUALIZACIÓN</small><b>En vivo</b></span>`;
  $("#task-list").innerHTML = filtered.map((task) => `<button class="task-card ${state.selectedTask === task.id ? "selected" : ""}" data-task="${esc(task.id)}"><i class="status-mark status-${esc(task.status)}"></i><span><strong>${task.parent_task_id ? "↳ " : ""}${esc(task.goal)}</strong><small>${task.parent_task_id ? "SUBTASK · " : ""}${shortId(task.id)} · ${date(task.created_at)} · ${esc(task.priority_label || "Medium")}</small></span><em class="status-${esc(task.status)}">${esc(task.status)}</em></button>`).join("") || `<div class="empty">No hay tareas persistidas.</div>`;
  $("#task-list").querySelectorAll("[data-task]").forEach((button) => button.addEventListener("click", () => selectTask(button.dataset.task)));
}
function renderEvents(events) {
  $("#event-stream").innerHTML = events.slice(0, 80).map((event) => `<div class="event"><i></i><div><strong>${esc(event.event_type)}</strong></div><div><small>${shortId(event.task_id)}${event.node_id ? ` · nodo ${shortId(event.node_id)}` : ""}</small></div><time>${date(event.created_at)}</time></div>`).join("") || `<div class="empty">Sin eventos.</div>`;
}
function renderResources(data) {
  const devices = (data.devices || []).map((device) => `<button class="resource resource-select" data-resource="device:${esc(device.name)}"><strong>${esc(device.name)} / ${esc(device.platform || "generic")}</strong><small>${esc(device.status)} · ${esc(device.transport || "local")}</small><p>${(device.capabilities || []).map(esc).join(" · ") || "Sin adaptador"}</p></button>`).join("");
  const projects = (data.projects || []).map((project) => `<button class="resource resource-select" data-resource="project:${esc(project.id)}"><strong>${esc(project.name)} / ${esc(project.project_type || "code")}</strong><small>${project.enabled ? "ACTIVO" : "PAUSADO"} · ${project.is_default ? "default" : "registrado"}</small><p>${esc(project.description || project.path)}</p></button>`).join("");
  const memories = (data.memories || []).map((memory) => `<button class="resource resource-select" data-resource="memory:${esc(memory.id)}"><strong>${esc(memory.key)}</strong><small>${esc(memory.kind)} · ${esc(memory.source || "USER")}</small><p>${esc(memoryPreview(memory.value))}</p></button>`).join("");
  $("#resource-summary").innerHTML = `<div class="resource-group"><p class="kicker">DISPOSITIVOS</p>${devices || "<div class=empty>Sin dispositivos.</div>"}</div><div class="resource-group"><p class="kicker">PROYECTOS DE CÓDIGO</p>${projects || "<div class=empty>Sin proyectos.</div>"}</div><div class="resource-group"><p class="kicker">MEMORIA · SQLITE</p><div class="resource-count">${data.memories.length} registros persistidos en la BD</div>${memories || "<div class=empty>Sin memoria persistida.</div>"}</div>`;
  $("#resource-summary").querySelectorAll("[data-resource]").forEach((item) => item.addEventListener("click", () => renderResourceDetail(item.dataset.resource, data)));
}
function renderCollections(data) {
  const card = (item, kind) => `<button type="button" class="collection-card" data-resource-kind="${kind}" data-resource-id="${esc(item.name || item.id || item.key)}"><div class="collection-card-head"><span class="status-mark status-${esc(item.status || (item.enabled === false ? "PAUSED" : "READY"))}"></span><div><strong>${esc(item.name || item.key || item.id)}</strong><small>${esc(item.platform || item.project_type || item.kind || item.status || "ready")}</small></div></div><p>${esc(item.description || item.path || memoryPreview(item.value) || "Sin descripción")}</p><div class="collection-meta">${kind === "device" ? esc((item.capabilities || []).join(" · ")) : kind === "project" ? `${item.enabled === false ? "Pausado" : "Activo"} · ${esc(item.id)}` : `${esc(item.source || "USER")} · ${date(item.updated_at)}`}</div></button>`;
  $("#devices-list").innerHTML = (data.devices || []).map((item) => card(item, "device")).join("") || '<div class="empty">Sin dispositivos conectados.</div>';
  $("#projects-list").innerHTML = (data.projects || []).map((item) => card(item, "project")).join("") || '<div class="empty">Sin proyectos registrados.</div>';
  $("#memory-list").innerHTML = (data.memories || []).map((item) => card(item, "memory")).join("") || '<div class="empty">Sin memoria persistida.</div>';
  $("#memory-summary").innerHTML = `<div class="stat"><small>LARGO PLAZO</small><strong>${(data.memories || []).filter((item) => ["LONG_TERM", "FACT", "PREFERENCE"].includes(item.kind)).length}</strong></div><div class="stat"><small>OPERATIVA</small><strong>${(data.memories || []).filter((item) => !["LONG_TERM", "FACT", "PREFERENCE"].includes(item.kind)).length}</strong></div><div class="stat"><small>REGISTROS SQLITE</small><strong>${(data.memories || []).length}</strong></div>`;
  const bindCollection = (selector, target) => $(selector).querySelectorAll("[data-resource-kind]").forEach((item) => item.addEventListener("click", () => renderResourceDetail({ kind: item.dataset.resourceKind, id: item.dataset.resourceId }, data, target)));
  bindCollection("#devices-list", "#devices-detail");
  bindCollection("#projects-list", "#projects-detail");
  bindCollection("#memory-list", "#memory-detail");
}
function memoryPreview(value) { const text = typeof value === "string" ? value : JSON.stringify(value); return text.length > 160 ? `${text.slice(0, 157)}...` : text; }
function renderResourceDetail(resourceId, data, target = "#resource-detail") {
  const parsed = typeof resourceId === "string" ? resourceId.match(/^([^:]+):(.*)$/) : null;
  const { kind, id } = parsed ? { kind: parsed[1], id: parsed[2] } : resourceId;
  const resource = kind === "device" ? (data.devices || []).find((item) => item.name === id) : kind === "project" ? (data.projects || []).find((item) => item.id === id || item.name === id) : (data.memories || []).find((item) => item.id === id || item.key === id);
  if (!resource) return;
  if (kind === "memory") {
    $(target).className = "resource-detail";
    $(target).innerHTML = `<div class="resource-detail-head"><p class="kicker">MEMORIA PERSISTIDA</p><h3>${esc(resource.key)}</h3><small>${esc(resource.kind)} · ${esc(resource.source || "USER")} · confianza ${esc(resource.confidence)}</small></div><pre class="memory-value">${esc(json(resource.value))}</pre><div class="resource-facts"><span><small>USOS</small><b>${esc(resource.usage_count || 0)}</b></span><span><small>ACTUALIZADA</small><b>${esc(date(resource.updated_at))}</b></span><span><small>EXPIRA</small><b>${esc(date(resource.expires_at))}</b></span></div>`;
    return;
  }
  const toolNames = kind === "device" ? resource.capabilities || [] : (data.tools || []).filter((tool) => tool.permissions.some((permission) => permission.startsWith("project") || permission.startsWith("filesystem") || permission.startsWith("deployment"))).map((tool) => tool.name);
  const tools = (data.tools || []).filter((tool) => toolNames.includes(tool.name));
  $(target).className = "resource-detail";
  const facts = kind === "device" ? data.system || {} : { path: resource.path, type: resource.project_type, codegraph: resource.codegraph_version ? `v${resource.codegraph_version}` : "no actualizado", last_audit: date(resource.last_audited_at) };
  $(target).innerHTML = `<div class="resource-detail-head"><p class="kicker">${kind === "device" ? "DISPOSITIVO" : "PROYECTO"}</p><h3>${esc(resource.name)}</h3><small>${esc(resource.description || resource.path || "")}</small></div><div class="resource-facts">${Object.entries(facts).map(([key, value]) => `<span><small>${esc(key)}</small><b>${esc(value)}</b></span>`).join("")}</div>${kind === "project" ? `<div class="project-actions"><button class="button secondary project-audit">Auditar</button><button class="button secondary project-refresh">Actualizar grafo</button><button class="button danger project-delete" data-project-id="${esc(resource.id)}">Borrar proyecto y directorio</button></div>` : ""}<div class="resource-tools"><strong>HERRAMIENTAS DISPONIBLES</strong>${tools.map((tool) => `<div><b>${esc(tool.name)}</b><span>${esc(tool.description)}</span><small>${tool.methods.map(esc).join(" · ")}</small></div>`).join("") || "<span>Sin herramientas registradas.</span>"}</div>`;
  const deleteButton = $(`${target} .project-delete`);
  const projectAction = async (suffix, message) => { try { await api(`/projects/${encodeURIComponent(resource.id)}/${suffix}`, { method: "POST" }); toast(message); await load(); } catch (error) { toast(error.message, true); } };
  $(`${target} .project-audit`)?.addEventListener("click", () => projectAction("audit", "Auditoría solicitada"));
  $(`${target} .project-refresh`)?.addEventListener("click", () => projectAction("codegraph/refresh", "Actualización del grafo solicitada"));
  if (deleteButton) deleteButton.addEventListener("click", async () => {
    if (!window.confirm(`Borrar ${resource.name}, su directorio y su registro persistido?`)) return;
    try {
      await api(`/projects/${encodeURIComponent(resource.id)}`, { method: "DELETE" });
      state.selectedTask = null;
      $(target).className = "resource-detail empty";
      $(target).innerHTML = "<strong>Proyecto borrado</strong><span>El directorio y el registro de SQLite han sido eliminados.</span>";
      toast("Proyecto borrado");
      await load();
    } catch (error) { toast(error.message, true); }
  });
}
function renderChat(tasks) {
  const chats = tasks.filter((task) => task.source === "DASHBOARD_CHAT" || task.metadata?.interaction === "chat").slice(0, 20).reverse();
  $("#chat-history").innerHTML = chats.map((task) => `<div class="chat-turn"><small>TÚ · ${date(task.created_at)}</small><p>${esc(task.goal)}</p><div><small>ASSISTANT · ${esc(task.status)}</small><p>${esc(task.result_summary || "Procesando en la cola persistente.")}</p></div></div>`).join("") || `<div class="empty">La conversación aparecerá aquí.</div>`;
}
function fillProjects(projects) {
  const targets = `<option value="device:computer">Ordenador</option>` + projects.filter((project) => project.enabled).map((project) => `<option value="project:${esc(project.id)}">${esc(project.name)}${project.is_default ? " · default" : ""}</option>`).join("");
  [$("#task-target"), $("#chat-target")].forEach((select) => { if (!select) return; const current = select.value; select.innerHTML = targets; select.value = current || "device:computer"; });
  const options = `<option value="">Selecciona una tarea</option>` + (state.data?.tasks || []).map((task) => `<option value="${task.id}">${esc(task.goal)} · ${esc(task.status)}</option>`).join("");
  [$("#trace-task")].forEach((select) => { if (!select) return; const current = select.value; select.innerHTML = options; select.value = current || state.selectedTask || ""; });
}
function taskEvents(taskId) {
  return state.taskDetails[taskId]?.events
    || state.data?.task_events?.[taskId]
    || (state.data?.events || []).filter((event) => event.task_id === taskId);
}
function traceEvents(taskId) { return taskEvents(taskId); }
function objectValue(value) {
  if (value && typeof value === "object") return value;
  if (typeof value !== "string") return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_) {
    return {};
  }
}
function renderedPromptFrom(payload) {
  const request = objectValue(payload?.request);
  const nestedPrompt = objectValue(request.prompt);
  return request.rendered_instructions || request.rendered_prompt || request.prompt_preview || nestedPrompt.instructions || "";
}
function taskActivity(taskId) {
  const events = taskEvents(taskId);
  const event = events[events.length - 1];
  if (!event) return { label: "Esperando actividad", detail: "La tarea aún no ha emitido eventos." };
  const labels = {
    TASK_CREATED: "Tarea creada",
    ORCHESTRATOR_DECISION: `Orquestador: ${event.payload?.worker || "routing"}`,
    AGENT_DECISION: `Worker: ${event.payload?.decision_type || "decisión"}`,
    AGENT_DELEGATION_RESULT: "Delegación completada",
    TASK_DELEGATED: "Subtarea creada",
    WORKER_COMPLETED: "Worker completó su turno",
    TASK_PLANNED: "Planner preparando el grafo",
    LLM_REQUEST: `${event.payload?.role || "LLM"} preparando contexto`,
    LLM_RESPONSE: `${event.payload?.role || "LLM"} respondió`,
    NODE_STARTED: "Ejecutando nodo",
    TOOL_CALLED: `Ejecutando ${event.payload?.tool || "herramienta"}.${event.payload?.method || ""}`,
    TOOL_RESULT: "Resultado de herramienta recibido",
    NODE_VERIFIED: `Nodo verificado: ${event.payload?.decision || "resultado"}`,
    NODE_COMPLETED: "Nodo completado",
    LLM_ERROR: `Error LLM (${event.payload?.role || "modelo"})`,
    LLM_SKIPPED: `Llamada LLM omitida: ${event.payload?.reason || "sin motivo"}`,
    FINAL_RESPONSE_READY: "Informe final generado",
    FINAL_RESPONSE_STARTED: "Generando informe final",
    FINAL_RESPONSE_FAILED: "Falló la generación del informe final",
    FINAL_RESPONSE_FALLBACK: `Informe alternativo: ${event.payload?.reason || "sin motivo"}`,
    TASK_COMPLETED: "Tarea completada",
    TASK_FAILED: "Tarea fallida",
    REPLAN_FAILED: "Falló la replanificación",
    REPLAN_REQUESTED: "Replanteando la estrategia",
    RECOVERY_ANALYZED: "Estrategia de recuperación analizada",
    USER_INPUT_REQUIRED: "Se necesita información del usuario",
    WAITING_FOR_USER: "Esperando al usuario",
    BUDGET_EXHAUSTED: "Presupuesto agotado",
  };
  return { label: labels[event.event_type] || event.event_type, detail: date(event.created_at) };
}
function workflowGraph(taskId, task, nodes, edges, events) {
  const workflow = [];
  const add = (kind, title, detail, status = "READY", meta = "") => workflow.push({ kind, title, detail, status, meta });
  add("start", "Tarea creada", `${shortId(taskId)} · ${date(task.created_at)}`, task.status);
  const route = events.find((event) => event.event_type === "ORCHESTRATOR_DECISION");
  if (route) {
    const payload = route.payload || {};
    add("orchestrator", "ORCHESTRATOR", `${payload.intent || "general"} · ${payload.worker || "GENERAL_WORKER"}`, payload.stage === "BLOCK" ? "BLOCKED" : "SUCCEEDED", payload.reason || "Routing inicial");
  }
  if (task.metadata?.worker || task.metadata?.template) {
    add("worker", task.metadata.worker || "WORKER", `template: ${task.metadata.template || "general"}`, task.status, "Contexto de ejecución seleccionado");
  }
  nodes.forEach((node) => add("operation", node.description || node.type, `${node.type} · ${node.status}`, node.status, node.error || ""));
  const children = (state.data?.tasks || []).filter((item) => item.parent_task_id === taskId);
  children.forEach((child) => add("delegate", child.goal, `subtask · ${child.status}`, child.status, `hijo ${shortId(child.id)}`));
  const delegation = events.find((event) => event.event_type === "AGENT_DELEGATION_RESULT");
  if (delegation && !children.length) add("delegate", "Delegación solicitada", "Subtasks pendientes de persistir", "WAITING");
  if (["SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"].includes(task.status)) {
    add("finish", task.status === "SUCCEEDED" ? "Workflow finalizado" : "Workflow detenido", task.status, task.status, task.failure_reason || task.result_summary || "Estado terminal");
  } else {
    add("next", "Siguiente turno", task.status, task.status, "El runtime continuará desde el ledger");
  }
  return workflow.map((item, index) => `<div class="workflow-node workflow-${esc(item.kind)} status-${esc(item.status)}"><span>${String(index + 1).padStart(2, "0")}</span><div><strong>${esc(item.title)}</strong><small>${esc(item.detail)}</small>${item.meta ? `<em>${esc(item.meta)}</em>` : ""}</div></div>`).join("");
}
function renderTrace(taskId) {
  if (!taskId || !state.data) return;
  const events = traceEvents(taskId);
  const task = state.data.tasks.find((item) => item.id === taskId) || {};
  const latest = events[events.length - 1] || {};
  const phase = latest.payload?.phase || task.phase || task.status || "idle";
  const mode = latest.payload?.mode || task.metadata?.mode || "autonomous";
  const decision = latest.payload?.decision || latest.payload?.reason || task.failure_reason || "Sin decisión registrada";
  const timeline = events.slice(-8).map((event) => `<li><time>${date(event.created_at)}</time><strong>${esc(event.event_type)}</strong><span>${esc(event.payload?.decision || event.payload?.reason || "")}</span></li>`).join("");
  const cards = `<div class="observability-summary"><div><small>FASE ACTUAL</small><strong>${esc(phase)}</strong></div><div><small>MODO</small><strong>${esc(mode)}</strong></div><div><small>DECISIÓN</small><strong>${esc(decision)}</strong></div></div><section class="panel timeline-panel"><header><strong>Línea de tiempo</strong><small>${events.length} eventos</small></header><ol>${timeline || '<li class="empty">Sin eventos.</li>'}</ol></section>`;
  $("#trace-workspace").innerHTML = cards + events.map((event, index) => {
    const payload = event.payload || {}, isRequest = event.event_type === "LLM_REQUEST", isResponse = event.event_type === "LLM_RESPONSE", isError = ["LLM_ERROR", "LLM_SKIPPED"].includes(event.event_type);
    const request = isRequest ? objectValue(payload.request) : null;
    const response = isResponse ? (payload.response || payload) : payload;
    const renderedPrompt = isResponse ? renderedPromptFrom(payload) : "";
    const requestPayload = objectValue(payload.request);
    const prompt = "";
    const body = isRequest ? `<div class="trace-block"><label>PROMPT EFECTIVO ENVIADO AL LLM</label><pre>${esc(renderedPromptFrom(payload) || "El prompt efectivo no está disponible en la persistencia de este evento.")}</pre></div>` : isError ? `<div class="trace-block trace-error"><label>${event.event_type === "LLM_SKIPPED" ? "LLAMADA OMITIDA" : "ERROR REAL DEL PROVEEDOR"}</label><pre>${esc(json({ type: payload.error_type, error: payload.error || payload.reason }))}</pre></div>` : `<div class="trace-block"><label>${isResponse ? "RESPUESTA VALIDADA" : "DETALLE DEL EVENTO"}</label><pre>${esc(json(response))}</pre></div>`;
    const title = isRequest ? "REQUEST / prompt efectivo enviado" : isResponse ? "RESPONSE / respuesta recibida" : isError ? "ERROR / llamada fallida" : event.event_type.replaceAll("_", " ");
    const characterCount = payload.context_chars || payload.response_chars || requestPayload.prompt_chars || renderedPrompt.length || 0;
    const visibleCount = isRequest ? renderedPromptFrom(payload).length : 0;
    const visibleLabel = isRequest ? ` · visible ${visibleCount}` : "";
    return `<article class="llm-card ${isError ? "llm-error" : ""}"><header><span class="trace-number">${String(index + 1).padStart(2, "0")}</span><div><strong>${title}</strong><small>${esc(payload.role || (event.node_id ? `nodo ${shortId(event.node_id)}` : "tarea"))} · ${date(event.created_at)} · ${characterCount} caracteres${visibleLabel}</small></div><em>${isRequest ? "OUT" : isError ? "ERR" : isResponse ? "IN" : "LOG"}</em></header><div class="trace-meta"><span>${isRequest ? "Prompt efectivo enviado" : isResponse ? "Respuesta validada" : event.event_type}</span><span>${payload.usage ? `${payload.usage.prompt_eval_count || 0} prompt · ${payload.usage.eval_count || 0} response tokens` : "evento persistido"}</span></div>${body}${prompt}</article>`;
  }).join("") || `<div class="empty">Esta tarea aún no tiene intercambios LLM persistidos.</div>`;
}
function renderInspector(taskId) {
  const task = state.data?.tasks.find((item) => item.id === taskId); if (!task) return;
  const nodes = state.data.task_nodes[taskId] || [], edges = state.data.task_edges[taskId] || [], events = taskEvents(taskId), usage = (state.data.analytics?.task_usage || []).find((item) => item.id === taskId), finalResponse = task.final_response || task.metadata?.final_response || (task.result_summary ? { response_type: "execution_summary", title: "Resumen de ejecución", summary: task.result_summary, evidence: [], limitations: ["Esta tarea no conserva un informe LLM final."] } : null);
  const activity = taskActivity(taskId);
  const clarification = task.metadata?.clarification || {};
  const recovery = ["WAITING", "BLOCKED", "FAILED"].includes(task.status)
    ? `<section class="recovery-card"><p class="kicker">${task.status === "WAITING" ? "SIGUIENTE PASO" : "RECUPERACIÓN"}</p><strong>${esc(clarification.prompt || task.failure_reason || "La tarea necesita intervención para continuar.")}</strong><small>${task.status === "WAITING" ? "Aporta la información solicitada y el worker reanudará el nodo." : "Puedes pedir otra estrategia o aportar una solución concreta."}</small></section>`
    : "";
  const tokenValue = usage?.actual_tokens_available ? formatNumber(usage.actual_tokens) : `~${formatNumber(usage?.estimated_tokens || 0)}`;
  const tokenLabel = usage?.actual_tokens_available ? "TOKENS" : "TOKENS EST.";
  const graphRows = nodes.map((node, index) => {
    const incoming = edges.filter((edge) => edge.to_node === node.id).map((edge) => `${shortId(edge.from_node)} → ${edge.dependency_type}`).join(" · ");
    return `<div class="node-row graph-node"><span class="node-order">${String(index + 1).padStart(2, "0")}</span><i class="status-mark status-${esc(node.status)}"></i><div><strong>${esc(node.description)}</strong><small>${esc(node.type)} · ${esc(node.status)}${incoming ? ` · depende de ${esc(incoming)}` : " · nodo inicial"}${node.error ? ` · ${esc(node.error)}` : ""}</small></div></div>`;
  }).join("");
  const eventRows = events.slice().reverse().map((event) => `<div class="inspector-event"><span>${date(event.created_at)}</span><strong>${esc(event.event_type)}</strong><small>${event.node_id ? `nodo ${shortId(event.node_id)}` : "tarea"}${event.payload?.reason ? ` · ${esc(event.payload.reason)}` : ""}</small></div>`).join("");
  $("#task-inspector").className = "panel inspector";
  const report = finalResponse ? `<section class="final-report"><div class="final-report-head"><p class="kicker">RESPUESTA FINAL</p><span class="badge status-${esc(task.status)}">${esc(finalResponse.response_type || "report")}</span></div><h4>${esc(finalResponse.title || "Resultado de la tarea")}</h4><p>${esc(finalResponse.summary || "")}</p>${Object.entries(finalResponse.sections || {}).map(([title, items]) => `<div class="final-report-section"><strong>${esc(title)}</strong><ul>${(items || []).map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>`).join("")} ${(finalResponse.evidence || []).length ? `<div class="final-report-section"><strong>Evidencia</strong><ul>${finalResponse.evidence.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}${(finalResponse.limitations || []).length ? `<div class="final-report-section report-limitations"><strong>Limitaciones</strong><ul>${finalResponse.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}</section>` : "";
  const approvalNode = nodes.find((node) => ["WAITING_APPROVAL", "APPROVAL_REQUIRED"].includes(node.status)) || null;
  $("#task-inspector").innerHTML = `<div class="inspector-head"><div><p class="kicker">TASK INSPECTOR</p><h3>${esc(task.goal)}</h3><small>${shortId(task.id)} · ${esc(task.status)}</small></div><span class="badge status-${esc(task.status)}">${esc(task.status)}</span></div><div class="inspector-activity"><span class="activity-pulse"></span><div><strong>${esc(activity.label)}</strong><small>${esc(activity.detail)}</small></div></div>${recovery}${report}<div class="inspector-actions">${approvalNode ? `<button class="button primary" data-action="approval" data-node-id="${esc(approvalNode.id)}">Aprobar nodo</button>` : ""}${["WAITING", "BLOCKED"].includes(task.status) ? `<button class="button primary" data-action="input">Resolver</button>` : ""}${["BLOCKED", "FAILED"].includes(task.status) ? `<button class="button" data-action="replan">Replanificar</button>` : ""}${task.status === "WAITING" ? `<button class="button" data-action="resume">Reanudar</button>` : ""}${!["SUCCEEDED", "FAILED", "CANCELLED"].includes(task.status) ? `<button class="button ghost" data-action="cancel">Cancelar</button>` : ""}<button class="button ghost" data-action="trace">Ver traza</button></div><div class="usage-strip"><span>LLM <b>${usage?.llm_calls || 0}</b></span><span>TOOLS <b>${usage?.tool_calls || 0}</b></span><span>NODOS <b>${nodes.length}</b></span><span>${tokenLabel} <b>${tokenValue}</b></span></div><p class="kicker">GRAFO DE DECISIONES · ${edges.length} DEPENDENCIAS</p><div class="graph-canvas">${nodes.map((node, index) => `<div class="decision-node status-${esc(node.status)}"><span>${String(index + 1).padStart(2, "0")}</span><strong>${esc(node.description || node.type)}</strong><small>${esc(node.type)} · ${esc(node.status)}</small>${edges.filter((edge) => edge.to_node === node.id).map((edge) => `<i>← ${esc(nodes.find((candidate) => candidate.id === edge.from_node)?.description || shortId(edge.from_node))}</i>`).join("")}</div>`).join("") || "<div class=empty>Sin nodos planificados.</div>"}</div><p class="kicker inspector-events-title">HISTORIAL COMPLETO · ${events.length} EVENTOS</p><div class="inspector-events">${eventRows || "<div class=empty>Sin eventos.</div>"}</div>`;
  const workflowPanel = document.createElement("section");
  workflowPanel.className = "workflow-panel";
  workflowPanel.innerHTML = `<div class="workflow-head"><p class="kicker">WORKFLOW GRAPH</p><small>${events.length} eventos · ${nodes.length} nodos · ${(state.data?.tasks || []).filter((item) => item.parent_task_id === taskId).length} subtareas</small></div><div class="workflow-canvas">${workflowGraph(taskId, task, nodes, edges, events)}</div>`;
  $("#task-inspector").querySelector(".inspector-actions")?.after(workflowPanel);
  $("#task-inspector").querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => taskAction(button.dataset.action, task, button.dataset.nodeId)));
}
async function loadTaskDetail(taskId, force = false) {
  if (!taskId || (!force && state.taskDetails[taskId])) return;
  try {
    state.taskDetails[taskId] = await api(`/dashboard/tasks/${encodeURIComponent(taskId)}`);
    if (state.selectedTask === taskId) {
      renderInspector(taskId);
      renderTrace(taskId);
    }
  } catch (error) {
    toast(`No se pudo cargar la traza completa: ${error.message}`, true);
  }
}
function selectTask(taskId) {
  state.selectedTask = taskId;
  renderTasks(state.data.tasks);
  renderInspector(taskId);
  renderTrace(taskId);
  loadTaskDetail(taskId);
}
async function taskAction(action, task, nodeId = "") { try { if (action === "cancel") await api(`/tasks/${task.id}/cancel`, { method: "POST" }); if (action === "resume") await api(`/tasks/${task.id}/resume`, { method: "POST" }); if (action === "replan") await api(`/tasks/${task.id}/replan`, { method: "POST" }); if (action === "approval") await api(`/tasks/${task.id}/nodes/${nodeId}/approval`, { method: "POST", body: JSON.stringify({ approved: true }) }); if (action === "trace") { setView("trace"); return; } if (action === "input") { const prompt = task.metadata?.clarification?.prompt || "Input JSON para la tarea"; const value = window.prompt(`${prompt}\n\nFormato JSON`, "{}"); if (value === null) return; await api(`/tasks/${task.id}/input`, { method: "POST", body: JSON.stringify({ input: JSON.parse(value) }) }); } toast(action === "replan" ? "Nueva estrategia solicitada" : "Orden actualizada"); await load(); } catch (error) { toast(`No se pudo actualizar: ${error.message}`, true); } }
function formatNumber(value) { return new Intl.NumberFormat("es-ES").format(value || 0); }
function formatSeconds(value) { const seconds = Number(value || 0); return seconds < 60 ? `${seconds.toFixed(1)}s` : `${(seconds / 60).toFixed(1)}m`; }
function timingRatio(value, total) { return total ? Math.max(4, Number(value || 0) / total * 100) : 4; }
async function load() {
  try {
    const data = await api("/dashboard/data");
    render(data);
    if (state.selectedTask) await loadTaskDetail(state.selectedTask, true);
    $("#last-refresh").textContent = `Actualizado ${new Date().toLocaleTimeString("es-ES")}`;
  } catch (error) {
    toast(`No se pudo leer el runtime: ${error.message}`, true);
  }
}

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
$("#refresh").addEventListener("click", load);
$("#idle-toggle").addEventListener("click", async () => { try { const enabled = $("#idle-toggle").getAttribute("aria-pressed") !== "true"; renderIdle(await api("/runtime/idle", { method: "PUT", body: JSON.stringify({ enabled }) })); } catch (error) { toast(error.message, true); } });
$("#reset-memory").addEventListener("click", async () => { if (!window.confirm("Borrar tareas, grafos, eventos, leases, resultados y memoria? Los proyectos se conservan.")) return; try { const result = await api("/runtime/reset", { method: "POST" }); state.selectedTask = null; toast(`Estado limpio: ${result.total} registros eliminados`); await load(); } catch (error) { toast(error.message, true); } });
$("#task-filter").addEventListener("input", () => state.data && renderTasks(state.data.tasks));
$("#task-status-filter").addEventListener("change", () => state.data && renderTasks(state.data.tasks));
$("#trace-task").addEventListener("change", (event) => {
  state.selectedTask = event.target.value || null;
  renderTrace(state.selectedTask);
  loadTaskDetail(state.selectedTask);
});
$("#metrics-task-filter").addEventListener("change", () => state.data && renderPerformanceMetrics(state.data));
$("#new-task").addEventListener("click", () => $("#modal").classList.remove("hidden")); $("#close-modal").addEventListener("click", () => $("#modal").classList.add("hidden"));
$("#task-form").addEventListener("submit", async (event) => { event.preventDefault(); try { const [target_type, target_id] = $("#task-target").value.split(":"); const body = { goal: $("#goal").value, target_type, target_id, priority: $("#priority").value || "Medium" }; const task = await api("/tasks", { method: "POST", body: JSON.stringify(body) }); $("#modal").classList.add("hidden"); $("#goal").value = ""; await load(); selectTask(task.id); setView("tasks"); } catch (error) { toast(error.message, true); } });
async function streamChat(body) {
  let response = await fetch("/chat-fast", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) response = await fetch("/chat/fast", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok || !response.body) throw new Error("stream unavailable");
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let text = "";
  while (true) { const chunk = await reader.read(); if (chunk.done) break; text += decoder.decode(chunk.value, { stream: true }); const live = $("#chat-live"); if (live) live.textContent = text.replace(/^data:\s*/gm, "").trim(); }
  return text;
}
$("#chat-mode").addEventListener("change", (event) => { $("#chat-mode-help").textContent = event.target.value === "agent" ? "Puede operar tareas y la cola" : "Consulta y crea trabajo"; });
$("#chat-form").addEventListener("submit", async (event) => { event.preventDefault(); const message = $("#chat-message").value.trim(); if (!message) return; try { const [target_type, target_id] = $("#chat-target").value.split(":"); const body = { message, target_type, target_id }; $("#chat-live").textContent = "Enviando…"; if ($("#chat-mode").value === "agent") { const result = await sendAgentMessage(body); if (!result) return; $("#chat-live").textContent = result.message || "Operación completada"; toast(result.message || "Operación completada"); } else { try { await streamChat(body); } catch (_) { await api("/chat", { method: "POST", body: JSON.stringify(body) }); } $("#chat-live").textContent = "Consulta enviada a la cola"; toast("Consulta enviada"); } $("#chat-message").value = ""; await load(); } catch (error) { $("#chat-live").textContent = "No se pudo enviar"; toast(error.message, true); } });
async function sendAgentMessage(body) {
  let result = await api("/chat/agent", { method: "POST", body: JSON.stringify(body) });
  if (result.action === "confirmation_required") {
    if (!window.confirm(result.message)) return null;
    result = await api("/chat/agent", { method: "POST", body: JSON.stringify({ ...body, confirm: true }) });
  }
  return result;
}
document.querySelectorAll("[data-reset-memory]").forEach((button) => button.addEventListener("click", () => $("#reset-memory").click()));
setView("overview"); load(); window.setInterval(load, 60000);
