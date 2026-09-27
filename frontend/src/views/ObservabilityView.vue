<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import { date } from '@/utils/format'

const system = useSystemStore()
const data = ref(null)
let timer = null

const runtime = computed(() => data.value?.runtime ?? {})
const events = computed(() => data.value?.events ?? [])
const idle = computed(() => runtime.value.idle ?? {})

async function load() {
  try { data.value = await api.observability(200) }
  catch (error) { system.notify(error.message, 'error') }
}
async function toggleIdle() {
  try { const r = await api.setIdle(!idle.value.enabled); system.notify(r.enabled ? 'Idle activado' : 'Idle detenido', 'ok'); await load() }
  catch (e) { system.notify(e.message, 'error') }
}
async function reset() {
  if (!confirm('Esto borra todas las tareas, nodos, eventos y memoria. ¿Continuar?')) return
  try { const r = await api.resetRuntime(); system.notify(`Runtime reiniciado (${r.total} registros)`, 'ok'); await load() }
  catch (e) { system.notify(e.message, 'error') }
}
onMounted(() => { load(); timer = window.setInterval(load, 6000) })
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <div class="row" style="margin-bottom:14px">
    <button class="btn" @click="toggleIdle">{{ idle.enabled ? 'Detener idle' : 'Activar idle' }}</button>
    <span class="grow" />
    <button class="btn danger" @click="reset">Reiniciar runtime</button>
  </div>

  <div class="grid cols-3" style="margin-bottom:16px">
    <div class="card"><h2>Readiness</h2>
      <div class="metric"><small>LLM</small><b>{{ runtime.readiness ? 'Listo' : '—' }}</b></div>
      <div class="metric"><small>Idle activo</small><b>{{ idle.enabled ? 'Sí' : 'No' }}</b></div>
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

  <div class="card" style="padding:0;overflow:hidden">
    <table class="table">
      <thead><tr><th>Tiempo</th><th>Tipo</th><th>Tarea</th><th>Nodo</th></tr></thead>
      <tbody>
        <tr v-for="e in events" :key="e.id">
          <td class="muted" style="white-space:nowrap">{{ date(e.created_at) }}</td>
          <td>{{ e.event_type }}</td>
          <td class="mono muted">{{ e.task_id ? e.task_id.slice(0,8) : '—' }}</td>
          <td class="mono muted">{{ e.node_id ? e.node_id.slice(0,8) : '—' }}</td>
        </tr>
      </tbody>
    </table>
    <div v-if="!events.length" class="empty">Sin eventos.</div>
  </div>
</template>
