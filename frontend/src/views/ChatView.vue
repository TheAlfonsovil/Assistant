<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, streamChat } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import { date, isActive, shortId, REFRESH_INTERVAL_MS } from '@/utils/format'

const system = useSystemStore()
const router = useRouter()

const CHAT_SOURCES = ['DASHBOARD_CHAT', 'DASHBOARD_CHAT_FAST', 'DASHBOARD_AGENT']

const turns = ref([])
const message = ref('')
const mode = ref('chat')
const target = ref('device:computer')
const live = ref('')
const sending = ref(false)
const log = ref(null)
let controller = null
let poller = null

const modeHelp = computed(() =>
  mode.value === 'agent' ? 'Puede operar tareas y la cola' : 'Consulta y crea trabajo',
)

const targets = computed(() => [
  { value: 'device:computer', label: 'Ordenador' },
  ...system.projects
    .filter((p) => p.enabled)
    .map((p) => ({
      value: `project:${p.id}`,
      label: p.is_default ? `${p.name} · default` : p.name,
    })),
])

const hasOpenTurn = computed(() =>
  turns.value.some((t) => t.role === 'assistant' && isActive(t.status || '')),
)

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
    },
  ]
}

async function loadHistory() {
  try {
    const data = await api.listTasks(60)
    const chats = (data.tasks || []).filter(isChatTask).slice(0, 20).reverse()
    const restored = chats.flatMap(fromTask)
    const liveIds = new Set(turns.value.map((t) => t.task_id).filter(Boolean))
    turns.value = [...restored.filter((t) => !liveIds.has(t.task_id)), ...turns.value]
    if (turns.value.length) scrollToBottom()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

function pushAssistant(status) {
  turns.value.push({ role: 'assistant', text: '', status, task_id: null, live: true })
  return turns.value[turns.value.length - 1]
}
async function runChat(body) {
  const turn = pushAssistant('QUEUED')
  live.value = 'Conectando con la cola…'
  controller = new AbortController()
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
  }
}

async function runAgent(body) {
  const turn = pushAssistant('RUNNING')
  live.value = 'Ejecutando comando de agente…'
  let result = await api.chatAgent(body)
  if (result.action === 'confirmation_required') {
    if (!confirm(result.message || '¿Confirmar la operación?')) {
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
  sending.value = true
  turns.value.push({ role: 'user', text, created_at: new Date().toISOString(), status: 'QUEUED' })
  message.value = ''
  scrollToBottom()
  const [target_type, target_id] = target.value.split(':')
  const body = { message: text, target_type, target_id }
  try {
    if (mode.value === 'agent') await runAgent(body)
    else await runChat(body)
    system.refresh()
  } catch (error) {
    system.notify(error.message, 'error')
    live.value = 'No se pudo enviar'
  } finally {
    sending.value = false
    scrollToBottom()
  }
}

function stop() {
  if (controller) controller.abort()
  live.value = 'Flujo detenido; la tarea sigue viva en la cola'
}

function openTask(turn) {
  if (turn.task_id) router.push({ name: 'task-detail', params: { id: turn.task_id } })
}

onMounted(() => {
  loadHistory()
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
      <div v-if="!turns.length" class="empty">La conversación aparecerá aquí.</div>
      <div v-for="(turn, index) in turns" :key="index" class="bubble" :class="turn.role">
        <small>
          {{ turn.role === 'user' ? 'TÚ' : 'ASSISTANT' }}
          <template v-if="turn.created_at"> · {{ date(turn.created_at) }}</template>
          <template v-if="turn.status"> · {{ turn.status }}</template>
        </small>
        <p :class="{ muted: !turn.text }">{{ turn.text || (turn.live ? 'Pensando…' : '—') }}</p>
        <button v-if="turn.task_id" class="btn ghost compact" @click="openTask(turn)">
          Abrir tarea {{ shortId(turn.task_id) }}
        </button>
      </div>
    </div>

    <div class="chat-side">
      <span class="chat-live" aria-live="polite">{{ live || modeHelp }}</span>
      <button v-if="sending" class="btn ghost" @click="stop">Detener flujo</button>
    </div>

    <form class="chat-form" @submit.prevent="send">
      <textarea
        v-model="message"
        class="textarea"
        placeholder="¿Qué necesitas que haga?"
        aria-label="Mensaje"
        @keydown.enter.exact.prevent="send"
      />
      <div class="chat-controls">
        <select v-model="mode" class="select" aria-label="Modo del chat">
          <option value="chat">Consulta</option>
          <option value="agent">Agente</option>
        </select>
        <select v-model="target" class="select" aria-label="Destino">
          <option v-for="t in targets" :key="t.value" :value="t.value">{{ t.label }}</option>
        </select>
        <span class="grow" />
        <button class="btn primary" type="submit" :disabled="sending || !message.trim()">
          Enviar
        </button>
      </div>
      <p class="chat-hint">
        Agente: <code>crear objetivo</code>, <code>cancelar id</code>, <code>borrar id</code>,
        <code>reanudar id</code>, <code>replanificar id</code> o <code>listar</code>.
      </p>
    </form>
  </div>
</template>
