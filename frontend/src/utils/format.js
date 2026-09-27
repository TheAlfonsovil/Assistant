const STATUS_ORDER = [
  'RUNNING', 'PLANNING', 'QUEUED', 'READY', 'VERIFYING', 'FINALIZING', 'WAITING',
  'SUCCEEDED', 'FAILED', 'BLOCKED', 'CANCELLED',
]

export const ACTIVE_STATUSES = [
  'QUEUED', 'PLANNING', 'READY', 'RUNNING', 'VERIFYING', 'FINALIZING', 'WAITING',
]

export const TERMINAL_STATUSES = ['SUCCEEDED', 'FAILED', 'CANCELLED', 'BLOCKED']

export function isActive(status) {
  return ACTIVE_STATUSES.includes(status)
}

export function isIncident(status) {
  return status === 'FAILED' || status === 'BLOCKED'
}

export function statusTone(status) {
  if (status === 'SUCCEEDED') return 'ok'
  if (isIncident(status)) return 'error'
  if (status === 'CANCELLED') return 'muted'
  if (isActive(status)) return 'run'
  return 'muted'
}

export function orderStatuses(counts) {
  return STATUS_ORDER.filter((key) => counts[key]).map((key) => [key, counts[key]])
}

export function date(value) {
  if (!value) return '-'
  return new Date(value).toLocaleString('es-ES', { dateStyle: 'short', timeStyle: 'medium' })
}

export function shortId(value) {
  return value ? String(value).slice(0, 8) : '-'
}

export function formatNumber(value) {
  const n = Number(value ?? 0)
  return Math.abs(n) >= 1000 ? n.toLocaleString('es-ES') : String(n)
}

export function formatSeconds(value) {
  const n = Number(value ?? 0)
  return `${n.toFixed(2)}s`
}

export function prettyJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value)
  }
}
