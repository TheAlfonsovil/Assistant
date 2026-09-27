<script setup>
import { onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'

const system = useSystemStore()
const data = ref(null)
async function load() { try { data.value = await api.resources() } catch (e) { system.notify(e.message, 'error') } }
onMounted(load)
function tone(status) {
  const s = String(status).toLowerCase()
  if (['online', 'ready', 'connected', 'ok'].includes(s)) return 'ok'
  if (['error', 'offline', 'unavailable'].includes(s)) return 'error'
  return 'muted'
}
</script>

<template>
  <div v-if="!data" class="empty">Cargando recursos…</div>
  <template v-else>
    <div class="section-title">Ramas de dispositivo</div>
    <div class="grid cols-3" style="margin-bottom:20px">
      <div v-for="d in data.devices" :key="d.name" class="card">
        <div class="row"><h3 class="grow" style="margin:0">{{ d.name }}</h3>
          <span class="pill" :class="tone(d.status)"><span class="dot" />{{ d.status }}</span></div>
        <div class="muted" style="margin:8px 0">{{ d.description }}</div>
        <div><span class="chip">{{ d.platform }}</span><span class="chip">{{ d.transport }}</span></div>
        <div style="margin-top:8px">
          <span v-for="c in d.capabilities" :key="c" class="chip">{{ c }}</span>
          <span v-if="!d.capabilities?.length" class="muted">Sin capacidades expuestas</span>
        </div>
      </div>
    </div>

    <div class="section-title">Capacidades / herramientas ({{ data.tools?.length || 0 }})</div>
    <div class="grid cols-3">
      <div v-for="t in data.tools" :key="t.name" class="card">
        <div class="mono" style="font-weight:600">{{ t.name }}</div>
        <div class="muted" style="font-size:12px;margin-top:6px">{{ t.description }}</div>
      </div>
    </div>
  </template>
</template>
