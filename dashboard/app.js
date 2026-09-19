const state = { data: null, selectedTask: null, view: "overview" };
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
  const titles = { overview: "Resumen operativo", tasks: "Tareas persistentes", trace: "Conversaciones LLM", activity: "Actividad reciente", resources: "Recursos conectados", chat: "Chat persistente" };
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
  const active = tasks.filter((task) => ["QUEUED", "PLANNING", "READY", "RUNNING", "VERIFYING", "WAITING"].includes(task.status));
  const failed = tasks.filter((task) => ["FAILED", "BLOCKED"].includes(task.status));
  $("#hero-status").textContent = health.status || "UNKNOWN";
  $("#runtime-label").textContent = health.llm_ready ? "READY" : "DEGRADED";
  $("#health-dot").className = health.llm_ready ? "ready" : "warning";
  $("#runtime-dot").className = health.llm_ready ? "ready" : "warning";
  $("#stats").innerHTML = [["ACTIVAS", active.length], ["COMPLETADAS", tasks.filter((task) => task.status === "SUCCEEDED").length], ["INCIDENTES", failed.length], ["LLM CALLS", data.analytics?.estimated_tokens?.total ? formatNumber(data.analytics.estimated_tokens.total) : 0]].map(([label, value]) => `<div class="stat"><small>${label}</small><strong>${value}</strong></div>`).join("");
  renderMetrics(metrics); renderChart(data.status_counts || {}); renderAnalytics(data.analytics || {}); renderTasks(tasks); renderEvents(data.events || []); renderResources(data); renderChat(tasks); fillProjects(data.projects || []); renderIdle(runtime.idle || {});
  if (state.selectedTask) { renderInspector(state.selectedTask); renderTrace(state.selectedTask); }
}
function renderMetrics(metrics) {
  const values = [["Pasadas", metrics.passes], ["Despachos", metrics.tasks_dispatched], ["Errores", metrics.task_errors], ["Pasadas idle", metrics.idle_passes], ["Sin LLM", metrics.not_ready_passes], ["Errores runtime", metrics.runtime_errors]];
  $("#runtime-metrics").innerHTML = values.map(([label, value]) => `<div class="metric"><small>${label}</small><b>${value ?? 0}</b></div>`).join("");
}
function renderChart(counts) {
  const total = Math.max(1, Object.values(counts).reduce((sum, value) => sum + value, 0));
  const order = ["RUNNING", "PLANNING", "QUEUED", "READY", "VERIFYING", "WAITING", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED"];
  $("#status-chart").innerHTML = order.filter((key) => counts[key]).map((key) => `<div class="bar-line"><span>${key}</span><i><b style="width:${Math.max(5, counts[key] / total * 100)}%"></b></i><strong>${counts[key]}</strong></div>`).join("") || `<div class="empty">Sin tareas persistidas.</div>`;
}
function renderAnalytics(analytics) {
  const tokens = analytics.estimated_tokens || {}, throughput = analytics.throughput || {}, latency = analytics.latency || {};
  $("#analytics-note").textContent = analytics.model || "modelo configurado";
  $("#analytics-kpis").innerHTML = [["TOKENS EST.", formatNumber(tokens.total)], ["ÉXITO", `${throughput.success_rate ?? 0}%`], ["PREFILL", formatSeconds(latency.prefill_seconds)], ["GENERACIÓN", formatSeconds(latency.generation_seconds)], ["TOKENS/S", formatNumber(latency.generation_tokens_per_second)], ["REINTENTOS", throughput.retries ?? 0]].map(([label, value]) => `<div><small>${label}</small><b>${value}</b></div>`).join("");
  const phases = analytics.phase_counts || {};
  const phaseRows = Object.entries(phases).map(([phase, count]) => `<div class="analysis-row"><span>${esc(phase)}</span><b>${count} intercambios</b></div>`).join("");
  $("#llm-analysis").innerHTML = `<div class="timing-split"><div><small>PREFILL / entrada</small><i><b style="width:${timingRatio(latency.prefill_seconds, latency.prefill_seconds + latency.generation_seconds)}%"></b></i></div><div><small>GENERACIÓN / salida</small><i><b class="generation-bar" style="width:${timingRatio(latency.generation_seconds, latency.prefill_seconds + latency.generation_seconds)}%"></b></i></div></div>${phaseRows || `<div class="empty">Aún no hay actividad LLM.</div>`}`;
}
function renderTasks(tasks) {
  const filter = ($( "#task-filter")?.value || "").toLowerCase().trim();
  const filtered = tasks.filter((task) => !filter || `${task.goal} ${task.status} ${task.id}`.toLowerCase().includes(filter));
  $("#task-list").innerHTML = filtered.map((task) => `<button class="task-card ${state.selectedTask === task.id ? "selected" : ""}" data-task="${esc(task.id)}"><i class="status-mark status-${esc(task.status)}"></i><span><strong>${esc(task.goal)}</strong><small>${shortId(task.id)} · ${date(task.created_at)}</small></span><em class="status-${esc(task.status)}">${esc(task.status)}</em></button>`).join("") || `<div class="empty">No hay tareas persistidas.</div>`;
  $("#task-list").querySelectorAll("[data-task]").forEach((button) => button.addEventListener("click", () => selectTask(button.dataset.task)));
}
function renderEvents(events) {
  $("#event-stream").innerHTML = events.slice(0, 80).map((event) => `<div class="event"><i></i><div><strong>${esc(event.event_type)}</strong><small>${shortId(event.task_id)}${event.node_id ? ` · nodo ${shortId(event.node_id)}` : ""}</small></div><time>${date(event.created_at)}</time></div>`).join("") || `<div class="empty">Sin eventos.</div>`;
}
function renderResources(data) {
  const devices = (data.devices || []).map((device) => `<div class="resource"><strong>${esc(device.name)} / ${esc(device.platform || "generic")}</strong><small>${esc(device.status)} · ${esc(device.transport || "local")}</small><p>${(device.capabilities || []).map(esc).join(" · ") || "Sin adaptador"}</p></div>`).join("");
  const projects = (data.projects || []).map((project) => `<div class="resource-line"><span>${esc(project.name)}</span><small>${project.project_type} · ${project.enabled ? "activo" : "pausado"}</small></div>`).join("");
  $("#resource-summary").innerHTML = `<div class="resource-group"><p class="kicker">DISPOSITIVOS</p>${devices || "<div class=empty>Sin dispositivos.</div>"}</div><div class="resource-group"><p class="kicker">PROYECTOS DE CÓDIGO</p>${projects || "<div class=empty>Sin proyectos.</div>"}</div><div class="resource-group"><p class="kicker">MEMORIA</p><strong>${data.memories.length} registros persistidos</strong></div>`;
}
function renderChat(tasks) {
  const chats = tasks.filter((task) => task.source === "DASHBOARD_CHAT" || task.metadata?.interaction === "chat").slice(0, 20).reverse();
  $("#chat-history").innerHTML = chats.map((task) => `<div class="chat-turn"><small>TÚ · ${date(task.created_at)}</small><p>${esc(task.goal)}</p><div><small>ASSISTANT · ${esc(task.status)}</small><p>${esc(task.result_summary || "Procesando en la cola persistente.")}</p></div></div>`).join("") || `<div class="empty">La conversación aparecerá aquí.</div>`;
}
function fillProjects(projects) {
  [$("#project"), $("#chat-project")].forEach((select) => { if (!select) return; const current = select.value; select.innerHTML = `<option value="">${select.id === "project" ? "Resolver automáticamente" : "Sin proyecto"}</option>` + projects.filter((project) => project.enabled).map((project) => `<option value="${esc(project.id)}">${esc(project.name)}${project.is_default ? " · default" : ""}</option>`).join(""); select.value = current; });
  const trace = $("#trace-task"), current = trace.value; trace.innerHTML = `<option value="">Selecciona una tarea</option>` + (state.data?.tasks || []).map((task) => `<option value="${task.id}">${esc(task.goal)} · ${esc(task.status)}</option>`).join(""); trace.value = current || state.selectedTask || "";
}
function taskEvents(taskId) { return state.data?.task_events?.[taskId] || (state.data?.events || []).filter((event) => event.task_id === taskId); }
function llmEvents(taskId) { return taskEvents(taskId).filter((event) => ["LLM_REQUEST", "LLM_RESPONSE"].includes(event.event_type)); }
function renderTrace(taskId) {
  if (!taskId || !state.data) return;
  const events = llmEvents(taskId);
  $("#trace-workspace").innerHTML = events.map((event, index) => {
    const payload = event.payload || {}, isRequest = event.event_type === "LLM_REQUEST";
    const request = isRequest ? payload.context : null;
    const response = isRequest ? null : (payload.response || payload);
    const renderedPrompt = !isRequest && payload.request ? payload.request.rendered_instructions : null;
    const body = isRequest ? `<div class="trace-block"><label>CONTEXTO ESTRUCTURADO</label><pre>${esc(json(request))}</pre></div>` : `<div class="trace-block"><label>RESPUESTA VALIDADA</label><pre>${esc(json(response))}</pre></div>${renderedPrompt ? `<details class="trace-prompt"><summary>PROMPT RENDERIZADO REAL · ${payload.request.prompt_chars} caracteres</summary><pre>${esc(renderedPrompt)}</pre></details>` : ""}`;
    return `<article class="llm-card"><header><span class="trace-number">${String(index + 1).padStart(2, "0")}</span><div><strong>${isRequest ? "REQUEST / contexto enviado" : "RESPONSE / respuesta recibida"}</strong><small>${esc(payload.role || "LLM")} · ${date(event.created_at)} · ${payload.context_chars || payload.response_chars || 0} caracteres</small></div><em>${isRequest ? "OUT" : "IN"}</em></header><div class="trace-meta"><span>${isRequest ? "Contexto al proveedor" : "Decisión + prompt real"}</span><span>${payload.usage ? `${payload.usage.prompt_eval_count || 0} prompt · ${payload.usage.eval_count || 0} response tokens` : "telemetría no disponible"}</span></div>${body}</article>`;
  }).join("") || `<div class="empty">Esta tarea aún no tiene intercambios LLM persistidos.</div>`;
}
function renderInspector(taskId) {
  const task = state.data?.tasks.find((item) => item.id === taskId); if (!task) return;
  const nodes = state.data.task_nodes[taskId] || [], usage = (state.data.analytics?.task_usage || []).find((item) => item.id === taskId);
  $("#task-inspector").className = "panel inspector";
  $("#task-inspector").innerHTML = `<div class="inspector-head"><div><p class="kicker">TASK INSPECTOR</p><h3>${esc(task.goal)}</h3><small>${shortId(task.id)} · ${esc(task.status)}</small></div><span class="badge status-${esc(task.status)}">${esc(task.status)}</span></div><div class="inspector-actions">${["WAITING", "BLOCKED"].includes(task.status) ? `<button class="button primary" data-action="input">Resolver</button>` : ""}${task.status === "WAITING" ? `<button class="button" data-action="resume">Reanudar</button>` : ""}${!["SUCCEEDED", "FAILED", "CANCELLED"].includes(task.status) ? `<button class="button ghost" data-action="cancel">Cancelar</button>` : ""}<button class="button ghost" data-action="trace">Ver LLM trace</button></div><div class="usage-strip"><span>LLM <b>${usage?.llm_calls || 0}</b></span><span>TOOLS <b>${usage?.tool_calls || 0}</b></span><span>NODOS <b>${nodes.length}</b></span><span>TOKENS <b>${formatNumber(usage?.estimated_tokens || 0)}</b></span></div><p class="kicker">NODOS DEL GRAFO</p><div class="node-list">${nodes.map((node) => `<div class="node-row"><i class="status-mark status-${esc(node.status)}"></i><div><strong>${esc(node.description)}</strong><small>${esc(node.type)} · ${esc(node.status)}${node.error ? ` · ${esc(node.error)}` : ""}</small></div></div>`).join("") || "<div class=empty>Sin nodos.</div>"}</div>`;
  $("#task-inspector").querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => taskAction(button.dataset.action, task)));
}
function selectTask(taskId) { state.selectedTask = taskId; renderTasks(state.data.tasks); renderInspector(taskId); renderTrace(taskId); }
async function taskAction(action, task) { try { if (action === "cancel") await api(`/tasks/${task.id}/cancel`, { method: "POST" }); if (action === "resume") await api(`/tasks/${task.id}/resume`, { method: "POST" }); if (action === "trace") { setView("trace"); return; } if (action === "input") { const value = window.prompt("Input JSON para la tarea", "{}"); if (value === null) return; await api(`/tasks/${task.id}/input`, { method: "POST", body: JSON.stringify({ input: JSON.parse(value) }) }); } toast("Orden actualizada"); await load(); } catch (error) { toast(`No se pudo actualizar: ${error.message}`, true); } }
function formatNumber(value) { return new Intl.NumberFormat("es-ES").format(value || 0); }
function formatSeconds(value) { const seconds = Number(value || 0); return seconds < 60 ? `${seconds.toFixed(1)}s` : `${(seconds / 60).toFixed(1)}m`; }
function timingRatio(value, total) { return total ? Math.max(4, Number(value || 0) / total * 100) : 4; }
async function load() { try { const data = await api("/dashboard/data"); render(data); $("#last-refresh").textContent = `Actualizado ${new Date().toLocaleTimeString("es-ES")}`; } catch (error) { toast(`No se pudo leer el runtime: ${error.message}`, true); } }

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
$("#refresh").addEventListener("click", load);
$("#idle-toggle").addEventListener("click", async () => { try { const enabled = $("#idle-toggle").getAttribute("aria-pressed") !== "true"; renderIdle(await api("/runtime/idle", { method: "PUT", body: JSON.stringify({ enabled }) })); } catch (error) { toast(error.message, true); } });
$("#reset-memory").addEventListener("click", async () => { if (!window.confirm("Borrar tareas, grafos, eventos, leases, resultados y memoria? Los proyectos se conservan.")) return; try { const result = await api("/runtime/reset", { method: "POST" }); state.selectedTask = null; toast(`Estado limpio: ${result.total} registros eliminados`); await load(); } catch (error) { toast(error.message, true); } });
$("#task-filter").addEventListener("input", () => state.data && renderTasks(state.data.tasks));
$("#trace-task").addEventListener("change", (event) => { state.selectedTask = event.target.value || null; renderTrace(state.selectedTask); });
$("#new-task").addEventListener("click", () => $("#modal").classList.remove("hidden")); $("#close-modal").addEventListener("click", () => $("#modal").classList.add("hidden"));
$("#task-form").addEventListener("submit", async (event) => { event.preventDefault(); try { const body = { goal: $("#goal").value, priority: Number($("#priority").value || 0) }; if ($("#project").value) body.project_id = $("#project").value; const task = await api("/tasks", { method: "POST", body: JSON.stringify(body) }); $("#modal").classList.add("hidden"); $("#goal").value = ""; await load(); selectTask(task.id); setView("tasks"); } catch (error) { toast(error.message, true); } });
$("#chat-form").addEventListener("submit", async (event) => { event.preventDefault(); try { await api("/chat", { method: "POST", body: JSON.stringify({ message: $("#chat-message").value, project_id: $("#chat-project").value || null }) }); $("#chat-message").value = ""; toast("Consulta enviada a la cola"); await load(); } catch (error) { toast(error.message, true); } });
setView("overview"); load(); window.setInterval(load, 5000);
