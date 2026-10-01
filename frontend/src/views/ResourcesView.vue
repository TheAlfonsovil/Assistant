<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'

const system = useSystemStore()
const data = ref(null)
const capture = ref(null)
const capturing = ref(false)
const monitorChoice = ref(0)

const hardware = computed(() => data.value?.hardware || null)
const monitors = computed(() => hardware.value?.display?.monitors || [])
const capabilities = computed(() => hardware.value?.capabilities || {})
const vision = computed(() => data.value?.vision || null)

async function load() {
  try {
    data.value = await api.resources()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

async function shoot() {
  capturing.value = true
  try {
    capture.value = await api.captureScreen(monitorChoice.value)
    system.notify('Captura tomada', 'ok')
  } catch (error) {
    system.notify(error.message, 'error')
  } finally {
    capturing.value = false
  }
}

function bytes(value) {
  if (value === null || value === undefined) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = Number(value)
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

function tone(status) {
  const s = String(status).toLowerCase()
  if (['online', 'ready', 'connected', 'ok'].includes(s)) return 'ok'
  if (['error', 'offline', 'unavailable'].includes(s)) return 'error'
  return 'muted'
}

onMounted(load)
</script>

<template>
  <div v-if="!data" class="empty">Cargando recursos…</div>
  <template v-else>
    <div class="section-title">Este ordenador</div>
    <div class="grid cols-3" style="margin-bottom:20px">
      <div class="card">
        <div class="row">
          <h3 class="grow" style="margin:0">Hardware</h3>
          <span class="pill" :class="capabilities.screen_capture ? 'ok' : 'muted'">
            <span class="dot" />{{ capabilities.screen_capture ? 'captura disponible' : 'sin captura' }}
          </span>
        </div>
        <div class="muted mono" style="font-size:11px;margin:8px 0;line-height:1.7">
          <div>{{ hardware?.host?.hostname }} · {{ hardware?.host?.os }} {{ hardware?.host?.os_version }}</div>
          <div>{{ hardware?.host?.architecture }} · CPU lógicos: {{ hardware?.cpu?.logical_cores }}</div>
        </div>
        <div class="row" style="gap:16px;margin-top:10px;align-items:flex-start">
          <div style="min-width:140px">
            <div class="muted" style="font-size:11px">Memoria</div>
            <div style="font-weight:600">{{ bytes(hardware?.memory?.total_bytes) }}</div>
            <div class="muted" style="font-size:11px">
              {{ hardware?.memory ? `${hardware.memory.load_percent}% en uso` : 'no disponible' }}
            </div>
          </div>
          <div class="grow">
            <div class="muted" style="font-size:11px">Discos</div>
            <div v-for="d in hardware?.storage || []" :key="d.mount" style="font-size:12px">
              <span class="mono">{{ d.mount }}</span> · libre {{ bytes(d.free_bytes) }} de {{ bytes(d.total_bytes) }}
            </div>
            <div v-if="!hardware?.storage?.length" class="muted" style="font-size:12px">no disponible</div>
          </div>
        </div>
        <div style="margin-top:10px">
          <span class="chip">shell</span>
          <span class="chip">procesos</span>
          <span class="chip">ratón/teclado: {{ capabilities.input_control ? 'activo' : 'desactivado' }}</span>
        </div>
        <div v-if="!capabilities.input_control" class="muted" style="font-size:11px;margin-top:6px">
          {{ capabilities.input_control_hint }}
        </div>
      </div>

      <div class="card">
        <div class="row">
          <h3 class="grow" style="margin:0">Pantallas</h3>
          <span class="chip">{{ monitors.length }} detectadas</span>
        </div>
        <div class="muted" style="margin:8px 0;font-size:12px">
          Escritorio virtual: {{ hardware?.display?.virtual_desktop?.width }}×{{ hardware?.display?.virtual_desktop?.height }}
          desde ({{ hardware?.display?.virtual_desktop?.left }}, {{ hardware?.display?.virtual_desktop?.top }})
        </div>
        <div v-for="m in monitors" :key="m.index" class="row" style="gap:8px;margin-bottom:6px">
          <span class="mono" style="font-size:12px">{{ m.index }}</span>
          <span>{{ m.width }}×{{ m.height }}</span>
          <span v-if="m.primary" class="chip">principal</span>
          <span class="muted" style="font-size:11px">en ({{ m.left }}, {{ m.top }})</span>
        </div>
        <div v-if="!monitors.length" class="muted">Este host no reporta monitores.</div>
        <div class="muted" style="font-size:11px;margin-top:8px">{{ hardware?.display?.note }}</div>
      </div>

      <div class="card">
        <div class="row">
          <h3 class="grow" style="margin:0">Captura en vivo</h3>
          <select v-model.number="monitorChoice" class="select" style="max-width:150px">
            <option v-for="m in monitors" :key="m.index" :value="m.index">
              Pantalla {{ m.index }}{{ m.primary ? ' (principal)' : '' }}
            </option>
            <option v-if="!monitors.length" :value="0">Escritorio</option>
          </select>
          <button class="btn primary" :disabled="capturing" @click="shoot">
            {{ capturing ? 'Capturando…' : 'Capturar' }}
          </button>
        </div>
        <div v-if="capture" style="margin-top:10px">
          <img
            :src="api.screenshotUrl(capture.filename)"
            alt="Captura de pantalla"
            style="width:100%;border-radius:8px;border:1px solid var(--line, #2a2a35)"
          />
          <div class="muted mono" style="font-size:11px;margin-top:6px;line-height:1.6">
            <div>{{ capture.image_width }}×{{ capture.image_height }} px · escala {{ capture.scale }} · {{ bytes(capture.bytes) }}</div>
            <div>origen ({{ capture.screen?.left }}, {{ capture.screen?.top }}) · {{ capture.captured_at }}</div>
          </div>
        </div>
        <div v-else class="muted" style="font-size:12px;margin-top:8px">
          Usa la misma operación que un trabajador (`screen.capture`), así que lo que ves aquí es
          la evidencia que produciría una tarea.
        </div>
      </div>

      <div v-if="vision" class="card">
        <div class="row">
          <h3 class="grow" style="margin:0">Percepción</h3>
          <span class="pill" :class="vision.model_reads_images ? 'ok' : 'muted'">
            <span class="dot" />{{ vision.model_reads_images ? 'lee imágenes' : 'solo geometría' }}
          </span>
        </div>
        <div class="mono" style="font-size:12px;margin-top:8px">modelo: {{ vision.model }}</div>
        <div class="mono" style="font-size:12px">
          imágenes por petición: {{ vision.images_per_request }} · detalle: {{ vision.detail }} ·
          tope por imagen: {{ bytes(vision.max_image_bytes) }}
        </div>
        <div class="muted" style="font-size:12px;margin-top:8px">{{ vision.note }}</div>
      </div>
    </div>

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
        <div v-if="d.host" class="muted mono" style="font-size:11px;margin-top:10px;line-height:1.7">
          <div>{{ d.host.os_generation || d.host.os }} · {{ d.host.architecture }} · {{ d.host.hostname }}</div>
          <div>shell: {{ d.host.default_shell }} · scripts: {{ d.host.script_extension }}</div>
          <div>cwd: {{ d.host.working_directory }}</div>
        </div>
      </div>
    </div>

    <div class="section-title">Capacidades / herramientas ({{ data.tools?.length || 0 }})</div>
    <div class="grid cols-3">
      <div v-for="t in data.tools" :key="t.name" class="card">
        <div class="mono" style="font-weight:600">{{ t.name }}</div>
        <div class="muted" style="font-size:12px;margin-top:6px">{{ t.description }}</div>
        <div style="margin-top:6px">
          <span v-for="m in t.methods" :key="m" class="chip">{{ m }}</span>
        </div>
      </div>
    </div>
  </template>
</template>
