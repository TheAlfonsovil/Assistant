<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '@/api/client'
import StatusBadge from '@/components/StatusBadge.vue'
import { useSystemStore } from '@/stores/system'
import { date, isActive, prettyJson } from '@/utils/format'

const route = useRoute()
const system = useSystemStore()
const detail = ref(null)
let timer = null

const task = computed(() => detail.value?.task)

async function load() {
  try {
    detail.value = await api.getTask(route.params.id)
    if (!isActive(detail.value.task.status)) stopPoll()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}
function startPoll() { if (!timer) timer = window.setInterval(load, 2500) }
function stopPoll() { if (timer) { window.clearInterval(timer); timer = null } }

function shortLabel(n) { return n.title || n.name || (n.id ? String(n.id).slice(0, 8) : n.type) }

async function act(fn, label) {
  try { await fn(route.params.id); system.notify(label, 'ok'); await load(); startPoll() }
  catch (error) { system.notify(error.message, 'error') }
}

onMounted(() => { load(); startPoll() })
onUnmounted(stopPoll)
</script>

<template>
  <div v-if="!detail" class="empty">Cargando…</div>
  <template v-else>
    <div class="card" style="margin-bottom:16px">
      <div class="row">
        <div class="grow">
          <div class="row" style="gap:10px">
            <StatusBadge :status="task.status" />
            <span class="mono muted">{{ task.id }}</span>
          </div>
          <h2 style="text-transform:none;font-size:18px;color:var(--text);letter-spacing:0;margin-top:10px">{{ task.goal }}</h2>
          <p class="muted" style="margin:6px 0 0">{{ task.description || 'Sin descripción' }}</p>
        </div>
        <div class="row">
          <button class="btn" @click="act(api.resumeTask, 'Reanudada')" :disabled="!['WAITING','BLOCKED'].includes(task.status)">Reanudar</button>
          <button class="btn" @click="act(api.replanTask, 'Replanificada')" :disabled="!isActive(task.status)">Replanificar</button>
          <button class="btn danger" @click="act(api.cancelTask, 'Cancelada')" :disabled="!isActive(task.status)">Cancelar</button>
        </div>
      </div>
      <div class="row" style="margin-top:12px;gap:18px">
        <span class="muted">Fuente: <b>{{ task.source }}</b></span>
        <span class="muted">Prioridad: <b>{{ task.priority_label }}</b></span>
        <span class="muted">Creada: {{ date(task.created_at) }}</span>
        <span class="muted">Iniciada: {{ date(task.started_at) }}</span>
        <span class="muted">Finalizada: {{ date(task.finished_at) }}</span>
      </div>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h2>Nodos ({{ detail.nodes.length }})</h2>
        <div v-if="!detail.nodes.length" class="empty">Sin nodos</div>
        <div v-for="n in detail.nodes" :key="n.id" class="metric">
          <span class="mono muted" style="font-size:12px">{{ n.type }} · {{ shortLabel(n) }}</span>
          <StatusBadge :status="n.status" />
        </div>
      </div>
      <div class="card">
        <h2>Resultado final</h2>
        <div v-if="task.failure_reason" style="color:var(--error)">{{ task.failure_reason }}</div>
        <div v-else-if="task.final_response" style="white-space:pre-wrap">{{ task.final_response }}</div>
        <div v-else class="empty">Aún sin respuesta final</div>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h2>Eventos ({{ detail.events.length }})</h2>
      <div v-if="!detail.events.length" class="empty">Sin eventos</div>
      <pre v-else class="code">{{ prettyJson(detail.events.slice(-40)) }}</pre>
    </div>
  </template>
</template>
