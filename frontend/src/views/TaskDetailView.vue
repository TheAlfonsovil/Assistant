<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import StatusBadge from '@/components/StatusBadge.vue'
import WorkflowGraph from '@/components/WorkflowGraph.vue'
import { useSystemStore } from '@/stores/system'
import { date, isActive, REFRESH_INTERVAL_MS } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const system = useSystemStore()
const detail = ref(null)
const children = ref([])
let timer = null
let childrenLoadedAt = 0

const task = computed(() => detail.value?.task)

async function load(forceChildren = false) {
  try {
    detail.value = await api.getTask(route.params.id)
    if (!isActive(detail.value.task.status)) stopPoll()
    await loadChildren(forceChildren)
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

// Subagentes (tareas hijas) con su propio grafo, como en el resto del panel.
// Se refrescan como mucho una vez por minuto para no multiplicar requests en el polling.
async function loadChildren(force = false) {
  if (!force && Date.now() - childrenLoadedAt < REFRESH_INTERVAL_MS) return
  try {
    const data = await api.listTasks(200)
    const byParent = new Map()
    data.tasks.forEach((t) => {
      if (!t.parent_task_id) return
      if (!byParent.has(t.parent_task_id)) byParent.set(t.parent_task_id, [])
      byParent.get(t.parent_task_id).push(t)
    })
    const build = async (taskId, depth) => {
      if (depth > 3) return []
      const kids = (byParent.get(taskId) || []).slice(0, 6)
      return Promise.all(kids.map(async (child) => {
        let nodes = []
        let edges = []
        try {
          const graph = await api.getGraph(child.id)
          nodes = graph.nodes
          edges = graph.edges
        } catch (error) { /* hijo puede haberse borrado */ }
        return { task: child, nodes, edges, events: [], children: await build(child.id, depth + 1) }
      }))
    }
    children.value = await build(route.params.id, 1)
    childrenLoadedAt = Date.now()
  } catch (error) { /* el resumen de hijos es best-effort */ }
}

function startPoll() { if (!timer) timer = window.setInterval(load, REFRESH_INTERVAL_MS) }
function stopPoll() { if (timer) { window.clearInterval(timer); timer = null } }

function openTask(id) { router.push({ name: 'task-detail', params: { id } }) }

async function act(fn, label) {
  try { await fn(route.params.id); system.notify(label, 'ok'); await load(true); startPoll() }
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

    <div class="card" style="margin-top:16px">
      <div class="row" style="margin-bottom:10px">
        <h2 style="margin:0">Workflow</h2>
        <span class="grow" />
        <span class="muted" style="font-size:12px">
          {{ detail.events.length }} eventos · {{ detail.nodes.length }} nodos · {{ children.length }} subagentes
        </span>
      </div>
      <WorkflowGraph :task="task" :nodes="detail.nodes" :edges="detail.edges"
                     :events="detail.events" :children="children" @open-task="openTask" />
      <p class="muted" style="margin:10px 0 0;font-size:12px">
        El detalle de cada evento está en <router-link to="/observability" class="mono">Observabilidad</router-link>.
      </p>
    </div>

    <div class="grid cols-2" style="margin-top:16px">
      <div class="card">
        <h2>Resultado final</h2>
        <div v-if="task.failure_reason" style="color:var(--error)">{{ task.failure_reason }}</div>
        <div v-else-if="task.final_response" style="white-space:pre-wrap">{{ task.final_response }}</div>
        <div v-else class="empty">Aún sin respuesta final</div>
      </div>
      <div class="card">
        <h2>Últimos eventos</h2>
        <div v-if="!detail.events.length" class="empty">Sin eventos</div>
        <div v-for="e in detail.events.slice(-8)" :key="e.id" class="metric">
          <span class="mono muted" style="font-size:12px">{{ e.event_type }}<span v-if="e.node_id"> · {{ e.node_id.slice(0,8) }}</span></span>
          <small>{{ date(e.created_at) }}</small>
        </div>
        <p class="muted" style="margin:8px 0 0;font-size:12px">
          Ver detalle en <router-link to="/observability" class="mono">Observabilidad</router-link>.
        </p>
      </div>
    </div>
  </template>
</template>
