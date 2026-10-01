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
const title = ref('')
const goal = ref('')
const description = ref('')
const target = ref('device:computer')
const files = ref([])
const attachmentError = ref('')

// Keep in sync with ASSISTANT_ATTACHMENT_MAX_BYTES / _MAX_COUNT.
const MAX_ATTACHMENT_BYTES = 5_000_000
const MAX_ATTACHMENTS = 4

const targets = computed(() => [
  { value: 'device:computer', label: 'Ordenador' },
  { value: '', label: 'Sin proyecto' },
  ...system.projects
    .filter((p) => p.enabled)
    .map((p) => ({ value: p.id, label: p.is_default ? `${p.name} · default` : p.name })),
])

const canCreate = computed(() => title.value.trim().length > 0 && !loading.value)

function formatSize(bytes) {
  return bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`
}

// Recurring holders carry metadata.schedule (set by the `schedule` capability).
function everyLabel(schedule) {
  if (!schedule) return ''
  if (schedule.kind === 'daily' || schedule.at_hour != null) {
    const hour = String(schedule.at_hour ?? 0).padStart(2, '0')
    const minute = String(schedule.at_minute ?? 0).padStart(2, '0')
    return `${hour}:${minute} (${schedule.timezone || 'UTC'})`
  }
  const value = Number(schedule.every_seconds) || 0
  if (value >= 86400 && value % 86400 === 0) return `${value / 86400} d`
  if (value >= 3600 && value % 3600 === 0) return `${value / 3600} h`
  if (value >= 60 && value % 60 === 0) return `${value / 60} min`
  return `${value} s`
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result || '')
      resolve(result.slice(result.indexOf(',') + 1))
    }
    reader.onerror = () => reject(new Error(`No se pudo leer ${file.name}`))
    reader.readAsDataURL(file)
  })
}

function onFiles(event) {
  attachmentError.value = ''
  const selected = Array.from(event.target.files || [])
  if (selected.length > MAX_ATTACHMENTS) {
    attachmentError.value = `Máximo ${MAX_ATTACHMENTS} imágenes por tarea.`
    files.value = []
    event.target.value = ''
    return
  }
  const tooBig = selected.find((file) => file.size > MAX_ATTACHMENT_BYTES)
  if (tooBig) {
    attachmentError.value = `${tooBig.name} supera los 5 MB.`
    files.value = []
    event.target.value = ''
    return
  }
  files.value = selected
}

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
  if (!canCreate.value) return
  const payload = { title: title.value.trim() }
  const objective = goal.value.trim()
  if (objective) payload.goal = objective
  if (description.value.trim()) payload.description = description.value.trim()
  if (files.value.length) {
    try {
      payload.attachments = await Promise.all(
        files.value.map(async (file) => ({
          filename: file.name,
          content_type: file.type || null,
          data_base64: await readAsBase64(file),
        })),
      )
    } catch (error) {
      attachmentError.value = error.message
      return
    }
  }
  if (target.value === 'device:computer') {
    payload.target_type = 'device'
    payload.target_id = 'computer'
  } else if (target.value) {
    payload.project_id = target.value
  }
  try {
    const res = await api.createTask(payload)
    system.notify(`Tarea ${res.id.slice(0, 8)} creada`, 'ok')
    title.value = ''
    goal.value = ''
    description.value = ''
    files.value = []
    attachmentError.value = ''
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
      <input v-model="title" class="input grow" maxlength="200" placeholder="Título (obligatorio)…" @keyup.enter="create" />
    </div>
    <div class="row" style="margin-top:8px">
      <input v-model="goal" class="input grow" maxlength="10000" placeholder="Objetivo operativo (opcional; por defecto el título)…" />
    </div>
    <div class="row" style="margin-top:8px">
      <textarea v-model="description" class="textarea" maxlength="20000" placeholder="Descripción y detalles (opcional)…" />
    </div>
    <div class="row" style="margin-top:8px">
      <input
        class="input grow"
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp"
        multiple
        @change="onFiles"
      />
      <select v-model="target" class="select" style="max-width:220px">
        <option v-for="t in targets" :key="t.value" :value="t.value">{{ t.label }}</option>
      </select>
      <button class="btn primary" :disabled="!canCreate" @click="create">Lanzar</button>
    </div>
    <p v-if="attachmentError" class="muted">{{ attachmentError }}</p>
    <p v-else-if="files.length" class="muted">
      Adjuntos: {{ files.map((f) => `${f.name} (${formatSize(f.size)})`).join(' · ') }}
    </p>
    <p v-else class="muted">
      Se admiten hasta 4 imágenes (png, jpeg, gif o webp) de 5 MB como máximo.
    </p>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <table class="table">
      <thead>
        <tr><th>ID</th><th>Título / objetivo</th><th>Estado</th><th>Prioridad</th><th>Fuente</th><th>Actualizado</th></tr>
      </thead>
      <tbody>
        <tr v-for="t in tasks" :key="t.id" :class="{ selected: isIncident(t.status) }" @click="router.push({ name: 'task-detail', params: { id: t.id } })">
          <td class="mono">{{ shortId(t.id) }}</td>
          <td class="truncate">
            {{ t.title || t.goal }}
            <small v-if="t.metadata?.attachments?.length" class="muted">
              📎 {{ t.metadata.attachments.length }}
            </small>
            <small v-if="t.metadata?.schedule" class="muted">
              🔁 {{ everyLabel(t.metadata.schedule) }}
              <template v-if="t.metadata.schedule.runs"> · {{ t.metadata.schedule.runs }} ejec.</template>
            </small>
            <small v-if="t.goal && t.goal !== t.title" class="muted"> · {{ t.goal }}</small>
          </td>
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
