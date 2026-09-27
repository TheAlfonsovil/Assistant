<script setup>
import { computed } from 'vue'
import { date, shortId, statusTone } from '@/utils/format'

const props = defineProps({
  task: { type: Object, required: true },
  nodes: { type: Array, default: () => [] },
  edges: { type: Array, default: () => [] },
  events: { type: Array, default: () => [] },
  children: { type: Array, default: () => [] },
  nested: { type: Boolean, default: false },
})
defineEmits(['open-task'])

const glyphs = {
  start: '→', codegraph: '⌘', orchestrator: '◎', worker: '◌', task: '●',
  operation: '•', subtask: '└', delegate: '↳', finish: '✓', next: '→',
}

const codegraphReady = computed(() =>
  props.events.find((e) => e.event_type.endsWith('CODEGRAPH_READY')))
const codegraphFailed = computed(() =>
  props.events.find((e) => e.event_type.includes('CODEGRAPH') && e.event_type.includes('FAILED')))
const routeEvent = computed(() => props.events.find((e) => e.event_type === 'ORCHESTRATOR_DECISION'))
const worker = computed(() => props.task.metadata?.worker || props.task.metadata?.template)
const terminal = computed(() =>
  ['SUCCEEDED', 'FAILED', 'CANCELLED', 'BLOCKED'].includes(props.task.status))

// The node hierarchy flattened to rows (depth drives indentation) so the
// template does not need a recursive sub-component.
const forest = computed(() => {
  const byParent = new Map()
  props.nodes.forEach((node) => {
    const key = node.parent_node_id || ''
    if (!byParent.has(key)) byParent.set(key, [])
    byParent.get(key).push(node)
  })
  const ids = new Set(props.nodes.map((node) => node.id))
  const incoming = (node) => props.edges
    .filter((edge) => edge.to_node === node.id)
    .map((edge) => {
      const source = props.nodes.find((candidate) => candidate.id === edge.from_node)
      return source ? (source.description || source.type) : shortId(edge.from_node)
    })
  const rows = []
  const walk = (list, depth) => list.forEach((node) => {
    rows.push({
      node,
      depth,
      kind: node.type === 'SUBTASK' ? 'subtask' : node.type === 'TASK' ? 'task' : 'operation',
      incoming: incoming(node),
    })
    walk(byParent.get(node.id) || [], depth + 1)
  })
  walk(props.nodes.filter((node) => !node.parent_node_id || !ids.has(node.parent_node_id)), 0)
  return rows
})

function tone(status) { return statusTone(status) }
</script>

<template>
  <div class="wf-canvas">
    <div v-if="!nested" class="wf-node" :class="`tone-${tone(task.status)}`">
      <span class="wf-kind">{{ glyphs.start }}</span>
      <div>
        <strong>Tarea creada</strong>
        <small>{{ shortId(task.id) }} · {{ date(task.created_at) }}</small>
        <em>{{ task.status }}</em>
      </div>
    </div>

    <div v-if="!nested && codegraphReady" class="wf-node tone-ok">
      <span class="wf-kind">{{ glyphs.codegraph }}</span>
      <div>
        <strong>CODEGRAPH PREFLIGHT</strong>
        <small>{{ codegraphReady.payload?.file_count ?? 0 }} archivos indexados</small>
        <em>actualizado {{ date(codegraphReady.created_at) }}</em>
      </div>
    </div>
    <div v-if="!nested && codegraphFailed" class="wf-node tone-error">
      <span class="wf-kind">{{ glyphs.codegraph }}</span>
      <div>
        <strong>CODEGRAPH PREFLIGHT</strong>
        <small>índice no disponible</small>
        <em>{{ codegraphFailed.payload?.error || 'error desconocido' }}</em>
      </div>
    </div>

    <div v-if="!nested && routeEvent" class="wf-node"
         :class="routeEvent.payload?.stage === 'BLOCK' ? 'tone-error' : 'tone-ok'">
      <span class="wf-kind">{{ glyphs.orchestrator }}</span>
      <div>
        <strong>ORCHESTRATOR</strong>
        <small>{{ routeEvent.payload?.intent || 'general' }} · {{ routeEvent.payload?.worker || 'GENERAL_WORKER' }}</small>
        <em>{{ routeEvent.payload?.reason || 'Routing inicial' }}</em>
      </div>
    </div>

    <div v-if="worker" class="wf-node" :class="`tone-${tone(task.status)}`">
      <span class="wf-kind">{{ glyphs.worker }}</span>
      <div>
        <strong>{{ task.metadata?.worker || 'WORKER' }}</strong>
        <small>template: {{ task.metadata?.template || 'general' }}</small>
        <em>{{ nested ? `subagente ${shortId(task.id)}` : 'Contexto de ejecución seleccionado' }}</em>
      </div>
    </div>

    <div class="wf-forest">
      <div v-for="row in forest" :key="row.node.id" class="wf-row" :style="{ marginLeft: `${row.depth * 26}px` }">
        <div class="wf-node" :class="`tone-${tone(row.node.status)}`">
          <span class="wf-kind">{{ glyphs[row.kind] }}</span>
          <div>
            <strong :title="row.node.description">{{ row.node.description || row.node.type }}</strong>
            <small>{{ row.node.type }} · {{ row.node.status }}</small>
            <em v-if="row.node.error" class="wf-error">{{ row.node.error }}</em>
            <em v-else-if="row.incoming.length">← {{ row.incoming.join(' · ') }}</em>
          </div>
        </div>
      </div>
      <div v-if="!forest.length" class="empty">Sin nodos en el grafo.</div>
    </div>

    <div v-for="child in children" :key="child.task.id" class="wf-agent">
      <button type="button" class="wf-node wf-delegate" :class="`tone-${tone(child.task.status)}`"
              @click="$emit('open-task', child.task.id)">
        <span class="wf-kind">{{ glyphs.delegate }}</span>
        <div>
          <strong :title="child.task.goal">{{ child.task.goal }}</strong>
          <small>SUBAGENTE · {{ child.task.metadata?.worker || 'WORKER' }} · {{ child.task.status }}</small>
          <em>hijo {{ shortId(child.task.id) }}</em>
        </div>
      </button>
      <div class="wf-children">
        <WorkflowGraph :task="child.task" :nodes="child.nodes" :edges="child.edges"
                       :events="child.events" :children="child.children || []" nested />
      </div>
    </div>

    <div v-if="!nested" class="wf-node" :class="`tone-${tone(task.status)}`">
      <span class="wf-kind">{{ terminal ? glyphs.finish : glyphs.next }}</span>
      <div>
        <strong>{{ terminal ? (task.status === 'SUCCEEDED' ? 'Workflow finalizado' : 'Workflow detenido') : 'Siguiente turno' }}</strong>
        <small>{{ task.status }}</small>
        <em>{{ task.failure_reason || task.result_summary || (terminal ? 'Estado terminal' : 'El runtime continuará desde el ledger') }}</em>
      </div>
    </div>
  </div>
</template>
