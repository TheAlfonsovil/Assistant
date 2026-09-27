// Thin typed-ish wrapper over the Assistant JSON API (mounted under /api/v1).
const BASE = '/api/v1'

async function request(path, { method = 'GET', body, signal } = {}) {
  const response = await fetch(`${BASE}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })
  if (!response.ok) {
    const text = await response.text().catch(() => '')
    throw new Error(text || `HTTP ${response.status}`)
  }
  if (response.status === 204) return null
  return response.json()
}

export const api = {
  // system
  health: () => request('/health'),
  // dashboard read models
  overview: () => request('/overview'),
  observability: (limit = 200) => request(`/observability?limit=${limit}`),
  getEvent: (id) => request(`/observability/events/${id}`),
  getNode: (taskId, nodeId) => request(`/tasks/${taskId}/nodes/${nodeId}`),
  resources: () => request('/resources'),
  metrics: (limit = 500) => request(`/metrics?limit=${limit}`),
  // tasks
  listTasks: (limit = 100, status) =>
    request(`/tasks?limit=${limit}${status ? `&status=${status}` : ''}`),
  getTask: (id) => request(`/tasks/${id}`),
  createTask: (payload) => request('/tasks', { method: 'POST', body: payload }),
  cancelTask: (id) => request(`/tasks/${id}/cancel`, { method: 'POST' }),
  resumeTask: (id) => request(`/tasks/${id}/resume`, { method: 'POST' }),
  replanTask: (id) => request(`/tasks/${id}/replan`, { method: 'POST' }),
  redefineTask: (id, payload) => request(`/tasks/${id}/redefine`, { method: 'POST', body: payload }),
  deleteTask: (id) => request(`/tasks/${id}`, { method: 'DELETE' }),
  submitTaskInput: (id, payload) => request(`/tasks/${id}/input`, { method: 'POST', body: payload }),
  approveNode: (id, nodeId, approved) =>
    request(`/tasks/${id}/nodes/${nodeId}/approval`, { method: 'POST', body: { approved } }),
  getGraph: (id) => request(`/tasks/${id}/graph`),
  getEvents: (id) => request(`/tasks/${id}/events`),
  // projects
  listProjects: () => request('/projects'),
  createProject: (payload) => request('/projects', { method: 'POST', body: payload }),
  updateProject: (id, payload) => request(`/projects/${id}`, { method: 'PUT', body: payload }),
  deleteProject: (id) => request(`/projects/${id}`, { method: 'DELETE' }),
  auditProject: (id, runTests = false) => request(`/projects/${id}/audit?run_tests=${runTests}`, { method: 'POST' }),
  refreshCodegraph: (id) => request(`/projects/${id}/codegraph/refresh`, { method: 'POST' }),
  // memory
  listMemory: () => request('/memory'),
  createMemory: (payload) => request('/memory', { method: 'POST', body: payload }),
  memorySummary: () => request('/memory/summary'),
  exportMemory: () => request('/memory/export'),
  redactMemory: (id) => request(`/memory/${id}/redact`, { method: 'POST' }),
  deleteMemory: (id) => request(`/memory/${id}`, { method: 'DELETE' }),
  purgeExpired: () => request('/memory/purge-expired', { method: 'POST' }),
  // runtime
  getIdle: () => request('/runtime/idle'),
  setIdle: (enabled) => request('/runtime/idle', { method: 'PUT', body: { enabled } }),
  resetRuntime: () => request('/runtime/reset', { method: 'POST' }),
  // chat (non-streaming + agent)
  chat: (payload) => request('/chat', { method: 'POST', body: payload }),
  chatAgent: (payload) => request('/chat/agent', { method: 'POST', body: payload }),
}

// Server-sent events for the streaming chat. Yields parsed {event,data} frames.
export async function* streamChat(payload, signal) {
  const response = await fetch(`${BASE}/chat/fast`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
  if (!response.ok || !response.body) {
    throw new Error((await response.text().catch(() => '')) || `HTTP ${response.status}`)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let index
    while ((index = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, index)
      buffer = buffer.slice(index + 2)
      const frame = { event: 'message', data: null }
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) frame.event = line.slice(6).trim()
        else if (line.startsWith('data:')) frame.data = line.slice(5).trim()
      }
      if (frame.data) {
        try {
          frame.data = JSON.parse(frame.data)
        } catch {
          /* keep raw data */
        }
      }
      yield frame
    }
  }
}

export default api
