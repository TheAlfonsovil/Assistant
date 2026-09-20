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
  const titles = { overview: "Resumen operativo", tasks: "Tareas persistentes", trace: "Observabilidad", activity: "Actividad reciente", resources: "Recursos conectados", chat: "Chat persistente" };
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
  renderMetrics(metrics); renderChart(data.status_counts || {}); renderAnalytics(data.analytics || {}); renderTasks(tasks); renderEvents(data.events || []); renderResources(data); renderChat(tasks); fillProjects(data.projects || []); renderIdle(runtime.idle || {});
  if (state.selectedTask) { renderInspector(state.selectedTask); renderTrace(state.selectedTask); }
}
function renderMetrics(metrics) {
  const values = [["Pasadas", metrics.passes], ["Despachos", metrics.tasks_dispatched], ["Errores", metrics.task_errors], ["Pasadas idle", metrics.idle_passes], ["Sin LLM", metrics.not_ready_passes], ["Errores runtime", metrics.runtime_errors]];
  $("#runtime-metrics").innerHTML = values.map(([label, value]) => `<div class="metric"><small>${label}</small><b>${value ?? 0}</b></div>`).join("");
  $("#runtime-passes").textContent = formatNumber(metrics.passes);
  $("#runtime-dispatches").textContent = formatNumber(metrics.tasks_dispatched);
  $("#runtime-errors").textContent = formatNumber(metrics.task_errors + metrics.runtime_errors);
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
  $("#analytics-kpis").innerHTML = [[tokenLabel, formatNumber(tokens.total)], ["ÉXITO", `${throughput.success_rate ?? 0}%`], ["PREFILL", formatSeconds(latency.prefill_seconds)], ["GENERACIÓN", formatSeconds(latency.generation_seconds)], ["TOKENS/S", formatNumber(latency.generation_tokens_per_second)], ["REINTENTOS", throughput.retries ?? 0]].map(([label, value]) => `<div><small>${label}</small><b>${value}</b></div>`).join("");
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
  const devices = (data.devices || []).map((device) => `<button class="resource resource-select" data-resource="device:${esc(device.name)}"><strong>${esc(device.name)} / ${esc(device.platform || "generic")}</strong><small>${esc(device.status)} · ${esc(device.transport || "local")}</small><p>${(device.capabilities || []).map(esc).join(" · ") || "Sin adaptador"}</p></button>`).join("");
  const projects = (data.projects || []).map((project) => `<button class="resource resource-select" data-resource="project:${esc(project.id)}"><strong>${esc(project.name)} / ${esc(project.project_type || "code")}</strong><small>${project.enabled ? "ACTIVO" : "PAUSADO"} · ${project.is_default ? "default" : "registrado"}</small><p>${esc(project.description || project.path)}</p></button>`).join("");
  const memories = (data.memories || []).map((memory) => `<button class="resource resource-select" data-resource="memory:${esc(memory.id)}"><strong>${esc(memory.key)}</strong><small>${esc(memory.kind)} · ${esc(memory.source || "USER")}</small><p>${esc(memoryPreview(memory.value))}</p></button>`).join("");
  $("#resource-summary").innerHTML = `<div class="resource-group"><p class="kicker">DISPOSITIVOS</p>${devices || "<div class=empty>Sin dispositivos.</div>"}</div><div class="resource-group"><p class="kicker">PROYECTOS DE CÓDIGO</p>${projects || "<div class=empty>Sin proyectos.</div>"}</div><div class="resource-group"><p class="kicker">MEMORIA</p><div class="resource-count">${data.memories.length} registros persistidos</div>${memories || "<div class=empty>Sin memoria persistida.</div>"}</div>`;
  $("#resource-summary").querySelectorAll("[data-resource]").forEach((item) => item.addEventListener("click", () => renderResourceDetail(item.dataset.resource, data)));
}
function memoryPreview(value) { const text = typeof value === "string" ? value : JSON.stringify(value); return text.length > 160 ? `${text.slice(0, 157)}...` : text; }
function renderResourceDetail(resourceId, data) {
  const [kind, id] = resourceId.split(":");
  const resource = kind === "device" ? (data.devices || []).find((item) => item.name === id) : kind === "project" ? (data.projects || []).find((item) => item.id === id) : (data.memories || []).find((item) => item.id === id);
  if (!resource) return;
  if (kind === "memory") {
    $("#resource-detail").className = "resource-detail";
    $("#resource-detail").innerHTML = `<div class="resource-detail-head"><p class="kicker">MEMORIA PERSISTIDA</p><h3>${esc(resource.key)}</h3><small>${esc(resource.kind)} · ${esc(resource.source || "USER")} · confianza ${esc(resource.confidence)}</small></div><pre class="memory-value">${esc(json(resource.value))}</pre><div class="resource-facts"><span><small>USOS</small><b>${esc(resource.usage_count || 0)}</b></span><span><small>ACTUALIZADA</small><b>${esc(date(resource.updated_at))}</b></span><span><small>EXPIRA</small><b>${esc(date(resource.expires_at))}</b></span></div>`;
    return;
  }
  const toolNames = kind === "device" ? resource.capabilities || [] : (data.tools || []).filter((tool) => tool.permissions.some((permission) => permission.startsWith("project") || permission.startsWith("filesystem") || permission.startsWith("deployment"))).map((tool) => tool.name);
  const tools = (data.tools || []).filter((tool) => toolNames.includes(tool.name));
  $("#resource-detail").className = "resource-detail";
  const facts = kind === "device" ? data.system || {} : { path: resource.path, type: resource.project_type, codegraph: resource.codegraph_version ? `v${resource.codegraph_version}` : "no actualizado", last_audit: date(resource.last_audited_at) };
  $("#resource-detail").innerHTML = `<div class="resource-detail-head"><p class="kicker">${kind === "device" ? "DISPOSITIVO" : "PROYECTO"}</p><h3>${esc(resource.name)}</h3><small>${esc(resource.description || resource.path || "")}</small></div><div class="resource-facts">${Object.entries(facts).map(([key, value]) => `<span><small>${esc(key)}</small><b>${esc(value)}</b></span>`).join("")}</div><div class="resource-tools"><strong>HERRAMIENTAS DISPONIBLES</strong>${tools.map((tool) => `<div><b>${esc(tool.name)}</b><span>${esc(tool.description)}</span><small>${tool.methods.map(esc).join(" · ")}</small></div>`).join("") || "<span>Sin herramientas registradas.</span>"}</div>`;
}
function renderChat(tasks) {
  const chats = tasks.filter((task) => task.source === "DASHBOARD_CHAT" || task.metadata?.interaction === "chat").slice(0, 20).reverse();
  $("#chat-history").innerHTML = chats.map((task) => `<div class="chat-turn"><small>TÚ · ${date(task.created_at)}</small><p>${esc(task.goal)}</p><div><small>ASSISTANT · ${esc(task.status)}</small><p>${esc(task.result_summary || "Procesando en la cola persistente.")}</p></div></div>`).join("") || `<div class="empty">La conversación aparecerá aquí.</div>`;
}
function fillProjects(projects) {
  const targets = `<option value="device:computer">Ordenador</option>` + projects.filter((project) => project.enabled).map((project) => `<option value="project:${esc(project.id)}">${esc(project.name)}${project.is_default ? " · default" : ""}</option>`).join("");
  [$("#task-target"), $("#chat-target")].forEach((select) => { if (!select) return; const current = select.value; select.innerHTML = targets; select.value = current || "device:computer"; });
  const trace = $("#trace-task"), current = trace.value; trace.innerHTML = `<option value="">Selecciona una tarea</option>` + (state.data?.tasks || []).map((task) => `<option value="${task.id}">${esc(task.goal)} · ${esc(task.status)}</option>`).join(""); trace.value = current || state.selectedTask || "";
}
function taskEvents(taskId) { return state.data?.task_events?.[taskId] || (state.data?.events || []).filter((event) => event.task_id === taskId); }
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
  return request.rendered_instructions || request.rendered_prompt || nestedPrompt.instructions || "";
}
function taskActivity(taskId) {
  const events = taskEvents(taskId);
  const event = events[events.length - 1];
  if (!event) return { label: "Esperando actividad", detail: "La tarea aún no ha emitido eventos." };
  const labels = {
    TASK_CREATED: "Tarea creada",
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
  };
  return { label: labels[event.event_type] || event.event_type, detail: date(event.created_at) };
}
function renderTrace(taskId) {
  if (!taskId || !state.data) return;
  const events = traceEvents(taskId);
  $("#trace-workspace").innerHTML = events.map((event, index) => {
    const payload = event.payload || {}, isRequest = event.event_type === "LLM_REQUEST", isResponse = event.event_type === "LLM_RESPONSE", isError = ["LLM_ERROR", "LLM_SKIPPED"].includes(event.event_type);
    const request = isRequest ? objectValue(payload.request) : null;
    const response = isResponse ? (payload.response || payload) : payload;
    const renderedPrompt = isResponse ? renderedPromptFrom(payload) : "";
    const requestPayload = objectValue(payload.request);
    const prompt = "";
    const body = isRequest ? `<div class="trace-block"><label>PROMPT EFECTIVO ENVIADO AL LLM</label><pre>${esc(request.rendered_instructions || request.rendered_prompt || objectValue(request.prompt).instructions || "No se ha persistido el prompt efectivo.")}</pre></div>` : isError ? `<div class="trace-block trace-error"><label>${event.event_type === "LLM_SKIPPED" ? "LLAMADA OMITIDA" : "ERROR REAL DEL PROVEEDOR"}</label><pre>${esc(json({ type: payload.error_type, error: payload.error || payload.reason }))}</pre></div>` : `<div class="trace-block"><label>${isResponse ? "RESPUESTA VALIDADA" : "DETALLE DEL EVENTO"}</label><pre>${esc(json(response))}</pre></div>`;
    const title = isRequest ? "REQUEST / prompt efectivo enviado" : isResponse ? "RESPONSE / respuesta recibida" : isError ? "ERROR / llamada fallida" : event.event_type.replaceAll("_", " ");
    const characterCount = payload.context_chars || payload.response_chars || requestPayload.prompt_chars || renderedPrompt.length || 0;
    return `<article class="llm-card ${isError ? "llm-error" : ""}"><header><span class="trace-number">${String(index + 1).padStart(2, "0")}</span><div><strong>${title}</strong><small>${esc(payload.role || (event.node_id ? `nodo ${shortId(event.node_id)}` : "tarea"))} · ${date(event.created_at)} · ${characterCount} caracteres</small></div><em>${isRequest ? "OUT" : isError ? "ERR" : isResponse ? "IN" : "LOG"}</em></header><div class="trace-meta"><span>${isRequest ? "Prompt efectivo enviado" : isResponse ? "Respuesta validada" : event.event_type}</span><span>${payload.usage ? `${payload.usage.prompt_eval_count || 0} prompt · ${payload.usage.eval_count || 0} response tokens` : "evento persistido"}</span></div>${body}${prompt}</article>`;
  }).join("") || `<div class="empty">Esta tarea aún no tiene intercambios LLM persistidos.</div>`;
}
function renderInspector(taskId) {
  const task = state.data?.tasks.find((item) => item.id === taskId); if (!task) return;
  const nodes = state.data.task_nodes[taskId] || [], edges = state.data.task_edges[taskId] || [], events = taskEvents(taskId), usage = (state.data.analytics?.task_usage || []).find((item) => item.id === taskId), finalResponse = task.final_response || task.metadata?.final_response || (task.result_summary ? { response_type: "execution_summary", title: "Resumen de ejecución", summary: task.result_summary, evidence: [], limitations: ["Esta tarea no conserva un informe LLM final."] } : null);
  const activity = taskActivity(taskId);
  const tokenValue = usage?.actual_tokens_available ? formatNumber(usage.actual_tokens) : `~${formatNumber(usage?.estimated_tokens || 0)}`;
  const tokenLabel = usage?.actual_tokens_available ? "TOKENS" : "TOKENS EST.";
  const graphRows = nodes.map((node, index) => {
    const incoming = edges.filter((edge) => edge.to_node === node.id).map((edge) => `${shortId(edge.from_node)} → ${edge.dependency_type}`).join(" · ");
    return `<div class="node-row graph-node"><span class="node-order">${String(index + 1).padStart(2, "0")}</span><i class="status-mark status-${esc(node.status)}"></i><div><strong>${esc(node.description)}</strong><small>${esc(node.type)} · ${esc(node.status)}${incoming ? ` · depende de ${esc(incoming)}` : " · nodo inicial"}${node.error ? ` · ${esc(node.error)}` : ""}</small></div></div>`;
  }).join("");
  const eventRows = events.slice(-12).reverse().map((event) => `<div class="inspector-event"><span>${date(event.created_at)}</span><strong>${esc(event.event_type)}</strong><small>${event.node_id ? `nodo ${shortId(event.node_id)}` : "tarea"}</small></div>`).join("");
  $("#task-inspector").className = "panel inspector";
  const report = finalResponse ? `<section class="final-report"><div class="final-report-head"><p class="kicker">RESPUESTA FINAL</p><span class="badge status-${esc(task.status)}">${esc(finalResponse.response_type || "report")}</span></div><h4>${esc(finalResponse.title || "Resultado de la tarea")}</h4><p>${esc(finalResponse.summary || "")}</p>${Object.entries(finalResponse.sections || {}).map(([title, items]) => `<div class="final-report-section"><strong>${esc(title)}</strong><ul>${(items || []).map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>`).join("")} ${(finalResponse.evidence || []).length ? `<div class="final-report-section"><strong>Evidencia</strong><ul>${finalResponse.evidence.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}${(finalResponse.limitations || []).length ? `<div class="final-report-section report-limitations"><strong>Limitaciones</strong><ul>${finalResponse.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}</section>` : "";
  $("#task-inspector").innerHTML = `<div class="inspector-head"><div><p class="kicker">TASK INSPECTOR</p><h3>${esc(task.goal)}</h3><small>${shortId(task.id)} · ${esc(task.status)}</small></div><span class="badge status-${esc(task.status)}">${esc(task.status)}</span></div><div class="inspector-activity"><span class="activity-pulse"></span><div><strong>${esc(activity.label)}</strong><small>${esc(activity.detail)}</small></div></div>${report}<div class="inspector-actions">${["WAITING", "BLOCKED"].includes(task.status) ? `<button class="button primary" data-action="input">Resolver</button>` : ""}${task.status === "WAITING" ? `<button class="button" data-action="resume">Reanudar</button>` : ""}${!["SUCCEEDED", "FAILED", "CANCELLED"].includes(task.status) ? `<button class="button ghost" data-action="cancel">Cancelar</button>` : ""}<button class="button ghost" data-action="trace">Ver traza</button></div><div class="usage-strip"><span>LLM <b>${usage?.llm_calls || 0}</b></span><span>TOOLS <b>${usage?.tool_calls || 0}</b></span><span>NODOS <b>${nodes.length}</b></span><span>${tokenLabel} <b>${tokenValue}</b></span></div><p class="kicker">RECORRIDO DEL GRAFO · ${edges.length} DEPENDENCIAS</p><div class="node-list">${graphRows || "<div class=empty>Sin nodos planificados.</div>"}</div><p class="kicker inspector-events-title">TRANSICIONES RECIENTES</p><div class="inspector-events">${eventRows || "<div class=empty>Sin eventos.</div>"}</div>`;
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
$("#task-form").addEventListener("submit", async (event) => { event.preventDefault(); try { const [target_type, target_id] = $("#task-target").value.split(":"); const body = { goal: $("#goal").value, target_type, target_id, priority: Number($("#priority").value || 0) }; const task = await api("/tasks", { method: "POST", body: JSON.stringify(body) }); $("#modal").classList.add("hidden"); $("#goal").value = ""; await load(); selectTask(task.id); setView("tasks"); } catch (error) { toast(error.message, true); } });
$("#chat-form").addEventListener("submit", async (event) => { event.preventDefault(); try { const [target_type, target_id] = $("#chat-target").value.split(":"); await api("/chat", { method: "POST", body: JSON.stringify({ message: $("#chat-message").value, target_type, target_id }) }); $("#chat-message").value = ""; toast("Consulta enviada a la cola"); await load(); } catch (error) { toast(error.message, true); } });
setView("overview"); load(); window.setInterval(load, 60000);
