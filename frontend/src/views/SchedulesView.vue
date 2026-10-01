<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import { date } from '@/utils/format'

const system = useSystemStore()
const router = useRouter()

const schedules = ref([])
const meta = ref({ default_timezone: 'UTC', common_timezones: ['UTC'], timezone_database: true })
const loading = ref(false)
const showForm = ref(false)
const editing = ref('')
const form = ref(blankForm())

function blankForm() {
  return {
    goal: '',
    title: '',
    description: '',
    project_id: '',
    mode: 'interval',
    amount: 30,
    unit: 60,
    at_hour: 8,
    at_minute: 0,
    timezone: '',
  }
}

const everySeconds = computed(() => {
  const amount = Math.max(1, Math.floor(Number(form.value.amount) || 1))
  return amount * Number(form.value.unit || 60)
})

const projects = computed(() => system.projects.filter((project) => project.enabled))

async function load() {
  loading.value = true
  try {
    const payload = await api.listSchedules()
    schedules.value = payload.schedules
    meta.value = payload
    if (!form.value.timezone) form.value.timezone = payload.default_timezone
  } catch (error) {
    system.notify(error.message, 'error')
  } finally {
    loading.value = false
  }
}

function payloadFromForm() {
  return {
    goal: form.value.goal.trim(),
    title: form.value.title.trim(),
    description: form.value.description.trim(),
    project_id: form.value.project_id || null,
    ...recurrenceFromForm(),
  }
}

// PUT only accepts recurrence: changing it must not rewrite the work itself.
function recurrenceFromForm() {
  if (form.value.mode === 'daily') {
    return {
      at_hour: Number(form.value.at_hour),
      at_minute: Number(form.value.at_minute),
      timezone: form.value.timezone,
    }
  }
  return { every_seconds: everySeconds.value, timezone: form.value.timezone }
}

async function submit() {
  if (!form.value.goal.trim()) {
    system.notify('El objetivo es obligatorio', 'error')
    return
  }
  try {
    if (editing.value) {
      await api.updateSchedule(editing.value, recurrenceFromForm())
      system.notify('Recurrencia actualizada', 'ok')
    } else {
      await api.createSchedule(payloadFromForm())
      system.notify('Horario creado', 'ok')
    }
    cancelEdit()
    await load()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

function edit(schedule) {
  editing.value = schedule.task_id
  showForm.value = true
  const unit = schedule.every_seconds && schedule.every_seconds % 86400 === 0 ? 86400
    : schedule.every_seconds && schedule.every_seconds % 3600 === 0 ? 3600 : 60
  form.value = {
    ...blankForm(),
    goal: schedule.goal,
    title: schedule.title,
    description: schedule.description,
    mode: schedule.kind === 'daily' ? 'daily' : 'interval',
    amount: schedule.every_seconds ? schedule.every_seconds / unit : 30,
    unit,
    at_hour: schedule.at_hour ?? 8,
    at_minute: schedule.at_minute ?? 0,
    timezone: schedule.timezone || meta.value.default_timezone,
  }
}

function cancelEdit() {
  editing.value = ''
  showForm.value = false
  form.value = { ...blankForm(), timezone: meta.value.default_timezone }
}

async function pause(schedule) {
  try {
    await api.pauseSchedule(schedule.task_id)
    system.notify('Horario pausado', 'ok')
    await load()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

async function resume(schedule) {
  try {
    await api.resumeSchedule(schedule.task_id)
    system.notify('Horario reanudado', 'ok')
    await load()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

async function cancel(schedule) {
  if (!confirm('¿Cancelar este horario? No se puede reanudar; para volver a programarlo habrá que crear uno nuevo.')) return
  try {
    await api.cancelSchedule(schedule.task_id)
    system.notify('Horario cancelado', 'ok')
    await load()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

function state(schedule) {
  if (schedule.cancelled) return { label: 'cancelado', css: 'muted' }
  if (schedule.paused) return { label: 'pausado', css: 'muted' }
  return { label: 'activo', css: 'ok' }
}

onMounted(load)
</script>

<template>
  <div class="row" style="margin-bottom:14px">
    <button class="btn primary" @click="editing ? cancelEdit() : (showForm = !showForm)">
      {{ editing ? 'Cancelar edición' : '＋ Nuevo horario' }}
    </button>
    <span class="grow" />
    <span class="muted" style="font-size:12px">Zona por defecto: {{ meta.default_timezone }}</span>
    <button class="btn ghost" @click="load">Actualizar</button>
  </div>

  <p v-if="meta.timezone_database === false" class="muted">
    Este equipo no tiene base de datos de zonas horarias: los horarios diarios solo
    aceptan <span class="mono">UTC</span> o un desplazamiento como
    <span class="mono">UTC+02:00</span>. Instala el paquete <span class="mono">tzdata</span>
    para usar nombres IANA.
  </p>

  <div v-if="showForm" class="card" style="margin-bottom:16px">
    <h2>{{ editing ? 'Editar recurrencia' : 'Programar trabajo recurrente' }}</h2>
    <div class="grid cols-2">
      <input v-model="form.title" class="input" placeholder="Título (opcional)" />
      <input v-model="form.goal" class="input" placeholder="Objetivo o instrucción (obligatorio)" />
      <input v-model="form.description" class="input" placeholder="Descripción (opcional)" />
      <select v-model="form.project_id" class="select" :disabled="!!editing">
        <option value="">Sin proyecto</option>
        <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
      </select>
    </div>

    <div class="row" style="margin-top:12px">
      <select v-model="form.mode" class="select" style="max-width:220px">
        <option value="interval">Cada cierto tiempo</option>
        <option value="daily">Cada día a una hora</option>
      </select>

      <template v-if="form.mode === 'interval'">
        <input v-model="form.amount" class="input" type="number" min="1" style="max-width:110px" />
        <select v-model="form.unit" class="select" style="max-width:140px">
          <option :value="60">minutos</option>
          <option :value="3600">horas</option>
          <option :value="86400">días</option>
        </select>
        <span class="muted" style="font-size:12px">cada {{ everySeconds }} s</span>
      </template>

      <template v-else>
        <input v-model="form.at_hour" class="input" type="number" min="0" max="23" style="max-width:90px" />
        <span>:</span>
        <input v-model="form.at_minute" class="input" type="number" min="0" max="59" style="max-width:90px" />
      </template>

      <input v-model="form.timezone" class="input" list="timezone-choices" placeholder="Zona horaria" style="max-width:220px" />
      <datalist id="timezone-choices">
        <option v-for="zone in meta.common_timezones" :key="zone" :value="zone" />
      </datalist>
    </div>

    <div class="row" style="margin-top:12px">
      <span class="muted" style="font-size:12px">
        El titular queda en espera y el runtime crea una tarea nueva en cada vencimiento.
      </span>
      <span class="grow" />
      <button class="btn primary" @click="submit">{{ editing ? 'Guardar' : 'Programar' }}</button>
    </div>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <table class="table">
      <thead>
        <tr><th>Recurrencia</th><th>Trabajo</th><th>Estado</th><th>Próxima</th><th>Ejec.</th><th>Zona</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="s in schedules" :key="s.task_id">
          <td class="mono">{{ s.text }}</td>
          <td class="truncate">
            {{ s.title || s.goal }}
            <small v-if="s.title && s.goal" class="muted"> · {{ s.goal }}</small>
          </td>
          <td>
            <span class="pill" :class="state(s).css"><span class="dot" />{{ state(s).label }}</span>
          </td>
          <td class="muted">
            {{ s.enabled ? (s.next_run_local || date(s.next_run_at)) : '—' }}
          </td>
          <td class="muted">{{ s.runs }}</td>
          <td class="muted">{{ s.timezone }}</td>
          <td class="row">
            <button v-if="!s.cancelled" class="btn ghost" @click="edit(s)">Editar</button>
            <button v-if="s.enabled" class="btn" @click="pause(s)">Pausar</button>
            <button v-else-if="s.paused" class="btn" @click="resume(s)">Reanudar</button>
            <button v-if="!s.cancelled" class="btn danger" @click="cancel(s)">Cancelar</button>
            <button class="btn ghost" @click="router.push({ name: 'task-detail', params: { id: s.task_id } })">Tarea</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="!schedules.length && !loading" class="empty">
      No hay trabajo recurrente. Programa el primero.
    </div>
  </div>
</template>
