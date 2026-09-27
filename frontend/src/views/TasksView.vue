<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import StatusBadge from '@/components/StatusBadge.vue'
import { date, shortId, isIncident } from '@/utils/format'

const router = useRouter()
const system = useSystemStore()
const tasks = ref([])
const loading = ref(false)
const filter = ref('')
const showForm = ref(false)
const goal = ref('')
const target = ref('device:computer')

const targets = computed(() => [
  { value: 'device:computer', label: 'Ordenador' },
  { value: '', label: 'Sin proyecto' },
  ...system.projects
    .filter((p) => p.enabled)
    .map((p) => ({ value: p.id, label: p.is_default ? `${p.name} · default` : p.name })),
])

async function load() {
  loading.value = true
  try {
    const data = await api.listTasks(200, filter.value || undefined)
    tasks.value = data.tasks
  } catch (error) {
    system.notify(error.message, 'error')
  } finally {
    loading.value = false
  }
}

async function create() {
  if (!goal.value.trim()) return
  const payload = { goal: goal.value.trim() }
  if (target.value === 'device:computer') {
    payload.target_type = 'device'
    payload.target_id = 'computer'
  } else if (target.value) {
    payload.project_id = target.value
  }
  try {
    const res = await api.createTask(payload)
    system.notify(`Tarea ${res.id.slice(0, 8)} creada`, 'ok')
    goal.value = ''
    await load()
    router.push({ name: 'task-detail', params: { id: res.id } })
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

onMounted(load)
</script>

<template>
  <div class="row" style="margin-bottom:14px">
    <button class="btn primary" @click="showForm = !showForm">＋ Nueva tarea</button>
    <select v-model="filter" class="select" style="max-width:220px" @change="load">
      <option value="">Todos los estados</option>
      <option v-for="s in ['RUNNING','QUEUED','PLANNING','WAITING','SUCCEEDED','FAILED','BLOCKED','CANCELLED']" :key="s">{{ s }}</option>
    </select>
    <span class="grow" />
    <button class="btn ghost" :disabled="loading" @click="load">Actualizar</button>
  </div>

  <div v-if="showForm" class="card" style="margin-bottom:16px">
    <h2>Crear tarea persistente</h2>
    <div class="row">
      <input v-model="goal" class="input grow" placeholder="Objetivo de la tarea…" @keyup.enter="create" />
      <select v-model="target" class="select" style="max-width:220px">
        <option v-for="t in targets" :key="t.value" :value="t.value">{{ t.label }}</option>
      </select>
      <button class="btn primary" @click="create">Lanzar</button>
    </div>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <table class="table">
      <thead>
        <tr><th>ID</th><th>Objetivo</th><th>Estado</th><th>Prioridad</th><th>Fuente</th><th>Actualizado</th></tr>
      </thead>
      <tbody>
        <tr v-for="t in tasks" :key="t.id" :class="{ selected: isIncident(t.status) }" @click="router.push({ name: 'task-detail', params: { id: t.id } })">
          <td class="mono">{{ shortId(t.id) }}</td>
          <td class="truncate">{{ t.goal }}</td>
          <td><StatusBadge :status="t.status" /></td>
          <td>{{ t.priority_label }}</td>
          <td class="muted">{{ t.source }}</td>
          <td class="muted">{{ date(t.updated_at) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-if="!tasks.length && !loading" class="empty">No hay tareas. Crea la primera.</div>
  </div>
</template>
