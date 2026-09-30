<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import { date, shortId, prettyJson, REFRESH_INTERVAL_MS } from '@/utils/format'

const system = useSystemStore()
const data = ref(null)
const expanded = ref(null) // { kind: 'event'|'node', eventId, data, loading }
let timer = null

const runtime = computed(() => data.value?.runtime ?? {})
const events = computed(() => data.value?.events ?? [])
const idle = computed(() => runtime.value.idle ?? {})
const offpeak = computed(() => runtime.value.offpeak ?? {})

function payloadSize(event) {
  const payload = event?.payload
  if (payload == null) return 0
  return typeof payload === 'string' ? payload.length : JSON.stringify(payload).length
}

function charLabel(count) {
  return count > 999 ? `${(count / 1000).toFixed(count > 9999 ? 0 : 1)}k` : String(count)
}

async function load() {
  try { data.value = await api.observability(200) }
  catch (error) { system.notify(error.message, 'error') }
}

function isExpanded(e) { return expanded.value?.eventId === e.id }

async function toggleEvent(e) {
  if (isExpanded(e)) { expanded.value = null; return }
  expanded.value = { kind: 'event', eventId: e.id, data: null, loading: true }
  try { expanded.value.data = await api.getEvent(e.id) }
  catch (error) { system.notify(error.message, 'error'); expanded.value = null; return }
  expanded.value.loading = false
}

async function toggleNode(e) {
  if (!e.node_id || !e.task_id) return
  if (expanded.value?.kind === 'node' && expanded.value.eventId === e.id) { expanded.value = null; return }
  expanded.value = { kind: 'node', eventId: e.id, data: null, loading: true }
  try { expanded.value.data = await api.getNode(e.task_id, e.node_id) }
  catch (error) { system.notify(error.message, 'error'); expanded.value = null; return }
  expanded.value.loading = false
}

async function toggleIdle() {
  try { const r = await api.setIdle(!idle.value.enabled); system.notify(r.enabled ? 'Idle activado' : 'Idle detenido', 'ok'); await load() }
  catch (e) { system.notify(e.message, 'error') }
}
async function toggleOffPeak() {
  try {
    const r = await api.setOffPeak(!offpeak.value.enabled)
    system.notify(
      r.enabled ? 'Ahorro de consumo activado: la cola se pausa en horas punta' : 'Ahorro de consumo desactivado',
      'ok',
    )
    await load()
  } catch (e) { system.notify(e.message, 'error') }
}
function offpeakHint() {
  if (!offpeak.value.configured) return 'no configurado'
  if (!offpeak.value.enabled) return 'desactivado'
  if (offpeak.value.state === 'PAUSED') return `en pausa · ${Math.round((offpeak.value.seconds_remaining || 0) / 60)} min restantes`
  return `activo · próxima ventana ${date(offpeak.value.next_change_at)}`
}
async function reset() {
  if (!confirm('Esto borra todas las tareas, nodos, eventos y memoria. ¿Continuar?')) return
  try { const r = await api.resetRuntime(); system.notify(`Runtime reiniciado (${r.total} registros)`, 'ok'); await load() }
  catch (e) { system.notify(e.message, 'error') }
}
onMounted(() => { load(); timer = window.setInterval(load, REFRESH_INTERVAL_MS) })
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <div class="row" style="margin-bottom:14px">
    <button class="btn" @click="toggleIdle">{{ idle.enabled ? 'Detener idle' : 'Activar idle' }}</button>
    <button class="btn" @click="toggleOffPeak">
      {{ offpeak.enabled ? 'Desactivar ahorro' : 'Activar ahorro de consumo' }}
    </button>
    <span class="grow" />
    <button class="btn danger" @click="reset">Reiniciar runtime</button>
  </div>

  <div class="grid cols-3" style="margin-bottom:16px">
    <div class="card"><h2>Readiness</h2>
      <div class="metric"><small>LLM</small><b>{{ runtime.readiness ? 'Listo' : '—' }}</b></div>
      <div class="metric"><small>Idle activo</small><b>{{ idle.enabled ? 'Sí' : 'No' }}</b></div>
      <div class="metric"><small>Ahorro de consumo</small><b>{{ offpeakHint() }}</b></div>
      <div class="metric"><small>Horas punta (UTC)</small><b>{{ (offpeak.peak_windows_utc || []).join(' · ') || '—' }}</b></div>
      <div class="metric"><small>Intervalo</small><b>{{ idle.interval_seconds ?? '—' }}s</b></div>
    </div>
    <div class="card"><h2>Mantenimiento</h2>
      <div class="metric"><small>Ejecuciones</small><b>{{ idle.maintenance_runs ?? 0 }}</b></div>
      <div class="metric"><small>Errores</small><b>{{ idle.maintenance_errors ?? 0 }}</b></div>
      <div class="metric"><small>Skips reentrantes</small><b>{{ idle.reentrant_skips ?? 0 }}</b></div>
      <div class="metric"><small>Última</small><b style="font-size:12px">{{ date(idle.last_maintenance_at) }}</b></div>
    </div>
    <div class="card"><h2>Último error</h2>
      <pre v-if="runtime.last_error" class="code" style="max-height:140px">{{ JSON.stringify(runtime.last_error, null, 2) }}</pre>
      <div v-else class="empty">Sin errores recientes</div>
    </div>
  </div>

  <div class="card table-scroll">
    <table class="table events-table">
      <thead><tr><th>Tiempo</th><th>Tipo</th><th>Tarea</th><th>Nodo</th><th class="right">Chars</th></tr></thead>
      <tbody>
        <template v-for="e in events" :key="e.id">
          <tr :class="{ selected: isExpanded(e) }" @click="toggleEvent(e)">
            <td class="muted" style="white-space:nowrap">{{ date(e.created_at) }}</td>
            <td>{{ e.event_type }}</td>
            <td class="mono muted">{{ e.task_id ? shortId(e.task_id) : '—' }}</td>
            <td>
              <span v-if="e.node_id" class="mono node-link" @click.stop="toggleNode(e)">{{ shortId(e.node_id) }}</span>
              <span v-else class="muted">—</span>
            </td>
            <td class="right mono muted" :title="`${payloadSize(e)} caracteres`">
              {{ charLabel(payloadSize(e)) }}
            </td>
          </tr>
          <tr v-if="isExpanded(e)">
            <td colspan="5" style="background:var(--bg-elev)">
              <div v-if="expanded.loading" class="empty">Cargando detalle…</div>
              <template v-else-if="expanded.kind === 'node'">
                <div class="row" style="margin-bottom:8px">
                  <span class="chip">{{ expanded.data.type }}</span>
                  <span class="chip">{{ expanded.data.status }}</span>
                  <span class="mono muted" style="font-size:12px">nodo {{ shortId(expanded.data.id) }}</span>
                  <span class="mono muted" style="font-size:12px">tarea {{ shortId(expanded.data.task_id) }}</span>
                </div>
                <p class="muted" style="margin:0 0 8px">{{ expanded.data.description }}</p>
                <pre class="code event-detail">{{ prettyJson(expanded.data) }}</pre>
              </template>
              <template v-else>
                <div class="row" style="margin-bottom:8px">
                  <span class="chip">{{ expanded.data.event_type }}</span>
                  <span class="mono muted" style="font-size:12px">evento {{ shortId(expanded.data.id) }}</span>
                  <span class="mono muted" style="font-size:12px">tarea {{ shortId(expanded.data.task_id) }}</span>
                  <span v-if="expanded.data.node_id" class="mono muted" style="font-size:12px">nodo {{ shortId(expanded.data.node_id) }}</span>
                  <span class="muted" style="font-size:12px">{{ date(expanded.data.created_at) }}</span>
                </div>
                <pre class="code event-detail">{{ prettyJson(expanded.data) }}</pre>
              </template>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
    <div v-if="!events.length" class="empty">Sin eventos.</div>
  </div>
</template>
