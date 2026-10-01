<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, streamChat } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import { date, isActive, prettyJson, shortId, statusTone, REFRESH_INTERVAL_MS } from '@/utils/format'

const system = useSystemStore()
const router = useRouter()

const CHAT_SOURCES = ['DASHBOARD_CHAT', 'DASHBOARD_CHAT_FAST', 'DASHBOARD_AGENT']
const TARGET_KEY = 'assistant.chat.target'
// Keep in sync with ASSISTANT_ATTACHMENT_MAX_BYTES / _MAX_COUNT.
const MAX_ATTACHMENT_BYTES = 5_000_000
const MAX_ATTACHMENTS = 4
// Payload keys worth showing without opening the raw event. They answer "what
// happened" for any tool, role or node, so the timeline needs no per-tool table.
const FACT_KEYS = ['tool', 'method', 'role', 'decision', 'status', 'success', 'duration', 'error']
// A device label is a courtesy name; anything without one shows its branch name,
// so a new branch appears here by itself the day it stops being a placeholder.
const DEVICE_LABELS = { computer: 'Ordenador' }

const turns = ref([])
const message = ref('')
const live = ref('')
const sending = ref(false)
const streaming = ref(false)
const files = ref([])
const attachmentError = ref('')
const devices = ref([])
const draftTarget = ref(localStorage.getItem(TARGET_KEY) || '')
const mention = ref({ open: false, query: '', index: 0 })
const log = ref(null)
let controller = null
let poller = null

function formatSize(bytes) {
  return bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`
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
    attachmentError.value = `Máximo ${MAX_ATTACHMENTS} imágenes por mensaje.`
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

async function attachmentPayload() {
  return Promise.all(
    files.value.map(async (file) => ({
      filename: file.name,
      content_type: file.type || null,
      data_base64: await readAsBase64(file),
    })),
  )
}

// The destination is a property of the message, not a mode of the screen: the
// default sits in a chip and a mention overrides it for that one message.
const targets = computed(() => {
  const items = []
  for (const device of devices.value) {
    if (device.status !== 'ACTIVE') continue
    items.push({
      value: `device:${device.name}`,
      label: DEVICE_LABELS[device.name] || device.name,
      kind: 'Dispositivo',
    })
  }
  if (!items.length) items.push({ value: 'device:computer', label: 'Ordenador', kind: 'Dispositivo' })
  for (const project of system.projects) {
    if (!project.enabled) continue
    items.push({
      value: `project:${project.id}`,
      label: project.name,
      kind: 'Proyecto',
      is_default: project.is_default,
    })
  }
  return items
})

const currentTarget = computed(() => {
  const chosen = targets.value.find((item) => item.value === draftTarget.value)
  // With nothing chosen yet, the project the deployment marked as default is the
  // useful answer; the machine itself is the fallback, not the first guess.
  return chosen || targets.value.find((item) => item.is_default) || targets.value[0]
})

const mentionMatches = computed(() => {
  const query = mention.value.query.toLowerCase()
  const matches = targets.value.filter((item) => !query || item.label.toLowerCase().includes(query))
  return matches.slice(0, 6)
})

function persistTarget(value) {
  draftTarget.value = value
  localStorage.setItem(TARGET_KEY, value)
}

function onType(event) {
  // A trailing "@word" opens the picker; anything else closes it.
  const match = /@([^\s@]*)$/.exec(message.value)
  mention.value.open = Boolean(match)
  mention.value.query = match ? match[1] : ''
  mention.value.index = 0
}

function applyMention(target) {
  message.value = message.value.replace(/@([^\s@]*)$/, `@${target.label} `)
  persistTarget(target.value)
  mention.value.open = false
}

function pickMention() {
  const choice = mentionMatches.value[mention.value.index]
  if (choice) applyMention(choice)
}

function moveMention(step) {
  const total = mentionMatches.value.length
  if (!total) return
  mention.value.index = (mention.value.index + step + total) % total
}

function resolveTarget(text) {
  // Longest label first, so "Assistant" wins over a project named "Assis".
  const ordered = [...targets.value].sort((a, b) => b.label.length - a.label.length)
  for (const target of ordered) {
    const marker = `@${target.label}`
    const at = text.toLowerCase().lastIndexOf(marker.toLowerCase())
    if (at === -1) continue
    const cleaned = (text.slice(0, at) + text.slice(at + marker.length)).replace(/\s{2,}/g, ' ').trim()
    const [type, id] = target.value.split(':')
    return { type, id, cleaned: cleaned || text }
  }
  const [type, id] = (currentTarget.value?.value || 'device:computer').split(':')
  return { type, id, cleaned: text }
}

const isCommand = computed(() => message.value.trim().startsWith('/'))

const hasOpenTurn = computed(() =>
  turns.value.some((turn) => turn.role === 'assistant' && isActive(turn.status || '')),
)

const attention = computed(() => {
  const map = {}
  for (const item of system.needsAttention) map[item.task_id] = item
  return map
})

function askFor(turn) {
  return turn.task_id ? attention.value[turn.task_id] : undefined
}

function scrollToBottom() {
  nextTick(() => {
    if (log.value) log.value.scrollTop = log.value.scrollHeight
  })
}

function isChatTask(task) {
  return CHAT_SOURCES.includes(task.source) || task.metadata?.interaction === 'chat'
}

function assistantText(task) {
  return (
    task.final_response ||
    task.result_summary ||
    task.failure_reason ||
    'Procesando en la cola persistente.'
  )
}

function fromTask(task) {
  return [
    { role: 'user', text: task.goal, task_id: task.id, created_at: task.created_at, status: task.status },
    {
      role: 'assistant',
      text: assistantText(task),
      task_id: task.id,
      created_at: task.finished_at || task.created_at,
      status: task.status,
      events: null,
      open: false,
    },
  ]
}

async function loadHistory() {
  try {
    const data = await api.listTasks(60)
    const chats = (data.tasks || []).filter(isChatTask).slice(0, 20).reverse()
    const restored = chats.flatMap(fromTask)
    const liveIds = new Set(turns.value.map((turn) => turn.task_id).filter(Boolean))
    turns.value = [...restored.filter((turn) => !liveIds.has(turn.task_id)), ...turns.value]
    if (turns.value.length) scrollToBottom()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

async function loadDevices() {
  try {
    const data = await api.resources()
    devices.value = data.devices || []
  } catch {
    devices.value = []
  }
}

function pushAssistant(status) {
  turns.value.push({ role: 'assistant', text: '', status, task_id: null, live: true, events: null, open: false })
  return turns.value[turns.value.length - 1]
}

// The process is the interesting half of an answer, and all of it is already
// recorded: the events say which tools ran, in which order, and what came back.
async function loadProcess(turn) {
  if (!turn.task_id || turn.events) return
  try {
    turn.events = (await api.getEvents(turn.task_id)) || []
  } catch (error) {
    turn.events = []
    live.value = error.message
  }
}

function toggleProcess(turn) {
  turn.open = !turn.open
  if (turn.open) loadProcess(turn)
}

function eventTone(event) {
  const payload = event.payload || {}
  const type = event.event_type || ''
  if (payload.success === false || /FAILED|EXHAUSTED|BLOCKED|CANCELLED/.test(type)) return 'error'
  if (/COMPLETED|SUCCEEDED|RESPONSE|PARSED|READY/.test(type)) return 'ok'
  return ''
}

function eventIcon(event) {
  const type = event.event_type || ''
  if (type.startsWith('TOOL')) return '⚙'
  if (type.startsWith('LLM')) return '∑'
  if (type.startsWith('NODE')) return '◆'
  if (type.startsWith('TASK')) return '▣'
  if (type.startsWith('RECOVERY')) return '♡'
  if (type.startsWith('SCHEDULE')) return '⏱'
  return '·'
}

function eventFacts(event) {
  const payload = event.payload || {}
  return FACT_KEYS.filter((key) => payload[key] !== undefined && payload[key] !== null && payload[key] !== '')
    .map((key) => `${key}=${typeof payload[key] === 'number' ? Number(payload[key]).toFixed(2) : payload[key]}`)
    .join('  ')
}

function counts(turn) {
  const events = turn.events || []
  return {
    tools: events.filter((event) => event.event_type === 'TOOL_RESULT').length,
    calls: events.filter((event) => event.event_type.startsWith('LLM_')).length,
    problems: events.filter((event) => eventTone(event) === 'error').length,
  }
}

async function runChat(body) {
  const turn = pushAssistant('QUEUED')
  live.value = 'Conectando con la cola…'
  controller = new AbortController()
  streaming.value = true
  let streamed = false
  try {
    for await (const frame of streamChat(body, controller.signal)) {
      streamed = true
      const data = typeof frame.data === 'object' && frame.data ? frame.data : {}
      if (frame.event === 'accepted') {
        turn.task_id = data.task_id || null
        turn.status = data.status || 'QUEUED'
        live.value = `Tarea ${shortId(data.task_id)} en cola`
      } else if (frame.event === 'status') {
        turn.status = data.status || turn.status
        live.value = `Estado: ${data.status}`
        if (data.error) turn.text = data.error
      } else if (frame.event === 'result') {
        turn.text = data.result || turn.text
        turn.status = data.status || turn.status
        live.value = 'Resultado recibido'
      } else if (frame.event === 'error') {
        turn.status = 'FAILED'
        turn.text = data.message || 'Error durante la ejecución'
        live.value = turn.text
      } else if (frame.event === 'done') {
        turn.status = data.status || turn.status
      }
      scrollToBottom()
    }
  } catch (error) {
    if (streamed || controller?.signal.aborted) {
      turn.text = turn.text || error.message
      live.value = error.message
    } else {
      // SSE unavailable: fall back to the non-streaming entry point.
      const task = await api.chat(body)
      turn.task_id = task.id
      turn.status = task.status
      turn.text = 'Consulta enviada a la cola persistente.'
      live.value = 'Consulta enviada a la cola'
    }
  } finally {
    turn.live = false
    controller = null
    streaming.value = false
    if (turn.task_id) await loadProcess(turn)
    scrollToBottom()
  }
}

async function runCommand(body) {
  const turn = pushAssistant('RUNNING')
  live.value = 'Ejecutando comando…'
  let result = await api.chatAgent(body)
  if (result.action === 'confirmation_required') {
    if (!window.confirm(result.message || '¿Confirmar la operación?')) {
      turns.value.pop()
      live.value = 'Operación cancelada'
      return
    }
    result = await api.chatAgent({ ...body, confirm: true })
  }
  turn.text = result.message || 'Operación completada'
  turn.task_id = result.task?.id || null
  turn.status = result.task?.status || 'SUCCEEDED'
  turn.live = false
  live.value = turn.text
  scrollToBottom()
}

async function send() {
  const text = message.value.trim()
  if (!text || sending.value) return
  // Decided before the box is cleared: the composer must look empty again while
  // the turn runs, and the message is what says whether this is a command.
  const command = text.startsWith('/')
  sending.value = true
  turns.value.push({ role: 'user', text, created_at: new Date().toISOString(), status: 'QUEUED' })
  message.value = ''
  mention.value.open = false
  scrollToBottom()
  const resolution = command ? { type: undefined, id: undefined, cleaned: text } : resolveTarget(text)
  const body = { message: resolution.cleaned }
  if (resolution.type) {
    body.target_type = resolution.type
    body.target_id = resolution.id
  }
  if (files.value.length) {
    try {
      body.attachments = await attachmentPayload()
    } catch (error) {
      attachmentError.value = error.message
      sending.value = false
      return
    }
  }
  try {
    if (command) await runCommand(body)
    else await runChat(body)
    files.value = []
    attachmentError.value = ''
    system.refresh()
  } catch (error) {
    system.notify(error.message, 'error')
    live.value = 'No se pudo enviar'
  } finally {
    sending.value = false
    scrollToBottom()
  }
}

function stopStream() {
  if (controller) controller.abort()
  live.value = 'Flujo detenido; la tarea sigue viva en la cola (usa Cancelar tarea).'
}

async function cancelTurn(turn) {
  if (!turn.task_id) return
  if (!window.confirm(`¿Cancelar la tarea ${shortId(turn.task_id)}?`)) return
  try {
    const task = await api.cancelTask(turn.task_id)
    turn.status = task?.status || 'CANCELLED'
    turn.text = turn.text || 'Tarea cancelada.'
    live.value = 'Tarea cancelada'
    system.refresh()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

async function answerQuestion(turn, item) {
  if (!item) return
  const value = (turn.answer || '').trim()
  if (!value) return
  await sendInput(turn, item, { answer: value })
  turn.answer = ''
}

async function answerWithProject(turn, item, option) {
  const project = system.projects.find((candidate) => candidate.name === option)
  if (project) {
    await sendInput(turn, item, { project_id: project.id })
    return
  }
  await sendInput(turn, item, { target_type: 'device', target_id: 'computer' })
}

async function sendInput(turn, item, input) {
  try {
    await api.submitTaskInput(turn.task_id, { node_id: item.node_id, input })
    live.value = 'Respuesta enviada; la tarea sigue en la cola'
    system.refresh()
    setTimeout(loadHistory, 1200)
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

function openTask(turn) {
  if (turn.task_id) router.push({ name: 'task-detail', params: { id: turn.task_id } })
}

onMounted(() => {
  loadHistory()
  loadDevices()
  poller = window.setInterval(() => {
    if (hasOpenTurn.value) loadHistory()
  }, REFRESH_INTERVAL_MS)
})
onUnmounted(() => {
  if (poller) window.clearInterval(poller)
  if (controller) controller.abort()
})
</script>

<template>
  <div class="chat">
    <div ref="log" class="chat-log">
      <div v-if="!turns.length" class="empty">
        Escribe qué necesitas. Menciona con <code>@</code> el proyecto o el dispositivo si no es el de por defecto.
      </div>

      <div v-for="(turn, index) in turns" :key="index" class="bubble" :class="turn.role">
        <small>
          {{ turn.role === 'user' ? 'TÚ' : 'ASSISTANT' }}
          <template v-if="turn.created_at"> · {{ date(turn.created_at) }}</template>
          <template v-if="turn.role === 'assistant' && turn.status">
            · <span class="tone" :class="statusTone(turn.status)">{{ turn.status }}</span>
          </template>
        </small>

        <p :class="{ muted: !turn.text }">
          {{ turn.text || (turn.live ? 'Pensando…' : '—') }}
        </p>

        <div v-if="askFor(turn)" class="ask">
          <strong>{{ askFor(turn).question }}</strong>
          <div class="ask-options">
            <template v-if="askFor(turn).kind === 'project_selection'">
              <button
                v-for="option in askFor(turn).options"
                :key="option"
                class="btn ghost compact"
                @click="answerWithProject(turn, askFor(turn), option)"
              >
                {{ option }}
              </button>
              <button class="btn ghost compact" @click="answerWithProject(turn, askFor(turn), null)">
                Ordenador (sin proyecto)
              </button>
            </template>
            <template v-else>
              <input
                v-model="turn.answer"
                class="input"
                placeholder="Tu respuesta"
                @keydown.enter.prevent="answerQuestion(turn, askFor(turn))"
              />
              <button class="btn primary compact" @click="answerQuestion(turn, askFor(turn))">
                Responder
              </button>
            </template>
          </div>
        </div>

        <template v-if="turn.role === 'assistant' && turn.task_id">
          <div class="proc">
            <button class="btn ghost compact" @click="toggleProcess(turn)">
              {{ turn.open ? 'Ocultar proceso' : 'Ver proceso' }}
            </button>
            <span v-if="turn.events" class="chat-live">
              {{ counts(turn).tools }} herramientas · {{ counts(turn).calls }} llamadas ·
              {{ counts(turn).problems }} incidencias
            </span>
          </div>
          <div v-if="turn.open" class="timeline">
            <p v-if="turn.events && !turn.events.length" class="chat-hint">Sin eventos registrados.</p>
            <details v-for="event in turn.events || []" :key="event.id" class="timeline-row" :class="eventTone(event)">
              <summary>
                <span class="timeline-icon">{{ eventIcon(event) }}</span>
                <code>{{ event.event_type }}</code>
                <span class="timeline-facts">{{ eventFacts(event) }}</span>
              </summary>
              <pre class="timeline-raw">{{ prettyJson(event.payload) }}</pre>
            </details>
          </div>
          <div class="row-actions">
            <button class="btn ghost compact" @click="openTask(turn)">Abrir tarea {{ shortId(turn.task_id) }}</button>
            <button v-if="isActive(turn.status || '')" class="btn ghost compact danger" @click="cancelTurn(turn)">
              Cancelar tarea
            </button>
          </div>
        </template>
      </div>
    </div>

    <div class="chat-side">
      <span class="chat-live" aria-live="polite">{{ live || 'Cada mensaje se ejecuta en la cola persistente.' }}</span>
      <button v-if="streaming" class="btn ghost" @click="stopStream">Detener flujo</button>
    </div>

    <form class="chat-form" @submit.prevent="send">
      <div class="composer">
        <div v-if="mention.open" class="mention">
          <button
            v-for="(item, i) in mentionMatches"
            :key="item.value"
            type="button"
            class="mention-item"
            :class="{ active: i === mention.index }"
            @click="applyMention(item)"
          >
            <span>{{ item.label }}</span>
            <em>{{ item.kind }}<template v-if="item.is_default"> · por defecto</template></em>
          </button>
          <p v-if="!mentionMatches.length" class="chat-hint">Sin coincidencias.</p>
        </div>
        <textarea
          v-model="message"
          class="textarea"
          placeholder="¿Qué necesitas? Escribe @ para elegir proyecto o dispositivo, o / para un comando de cola."
          aria-label="Mensaje"
          @input="onType"
          @keydown.enter.exact.prevent="mention.open ? pickMention() : send()"
          @keydown.down.exact.prevent="moveMention(1)"
          @keydown.up.exact.prevent="moveMention(-1)"
          @keydown.esc="mention.open = false"
        />
      </div>

      <div class="chat-controls">
        <span class="chip">destino: {{ currentTarget?.label }}</span>
        <input
          class="input"
          style="max-width: 210px"
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp"
          multiple
          aria-label="Adjuntar imágenes"
          @change="onFiles"
        />
        <span class="grow" />
        <button class="btn primary" type="submit" :disabled="sending || !message.trim()">Enviar</button>
      </div>

      <p v-if="attachmentError" class="chat-hint">{{ attachmentError }}</p>
      <p v-else-if="files.length" class="chat-hint">
        Adjuntos: {{ files.map((f) => `${f.name} (${formatSize(f.size)})`).join(' · ') }}
      </p>
      <p v-if="isCommand" class="chat-hint">
        Comando de cola: <code>crear</code>, <code>cancelar id</code>, <code>borrar id</code>,
        <code>reanudar id</code>, <code>replanificar id</code> o <code>listar</code>.
      </p>
    </form>
  </div>
</template>
