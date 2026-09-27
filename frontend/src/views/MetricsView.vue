<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import MetricCard from '@/components/MetricCard.vue'
import { formatNumber, formatSeconds } from '@/utils/format'

const system = useSystemStore()
const m = ref(null)
async function load() { try { m.value = await api.metrics(500) } catch (e) { system.notify(e.message, 'error') } }
onMounted(load)

const tokens = computed(() => m.value?.display_tokens ?? {})
const latency = computed(() => m.value?.latency ?? {})
const throughput = computed(() => m.value?.throughput ?? {})
function bars(obj) {
  const entries = Object.entries(obj || {}).sort((a, b) => b[1] - a[1]).slice(0, 8)
  const max = Math.max(1, ...entries.map(([, v]) => v))
  return entries.map(([k, v]) => ({ k, v, pct: (v / max) * 100 }))
}
const distTools = computed(() => bars(m.value?.distribution?.tools))
const distTasks = computed(() => bars(m.value?.distribution?.task_types))
const distNodes = computed(() => bars(m.value?.distribution?.node_types))
</script>

<template>
  <div v-if="!m" class="empty">Cargando métricas…</div>
  <template v-else>
    <div class="row" style="margin-bottom:14px">
      <span class="chip">modelo: {{ m.model }}</span>
      <span class="chip">tokens: {{ tokens.source }}</span>
      <span class="grow" /><button class="btn ghost" @click="load">Actualizar</button>
    </div>

    <div class="grid cols-4" style="margin-bottom:16px">
      <MetricCard label="Tokens prompt" :value="formatNumber(tokens.prompt)" />
      <MetricCard label="Tokens respuesta" :value="formatNumber(tokens.response)" />
      <MetricCard label="Éxito" :value="throughput.success_rate + '%'" :hint="throughput.completed_tasks + '/' + throughput.terminal_tasks + ' terminales'" />
      <MetricCard label="Reintentos" :value="throughput.retries ?? 0" />
    </div>

    <div class="grid cols-2" style="margin-bottom:16px">
      <div class="card"><h2>Latencia</h2>
        <div class="metric"><small>Tarea media</small><b>{{ formatSeconds(latency.average_task_seconds) }}</b></div>
        <div class="metric"><small>LLM medio</small><b>{{ formatSeconds(latency.average_llm_seconds) }}</b></div>
        <div class="metric"><small>Herramienta media</small><b>{{ formatSeconds(latency.average_tool_seconds) }}</b></div>
        <div class="metric"><small>Prefill</small><b>{{ formatSeconds(latency.prefill_seconds) }}</b></div>
        <div class="metric"><small>Generación</small><b>{{ formatSeconds(latency.generation_seconds) }}</b></div>
        <div class="metric"><small>tok/s generación</small><b>{{ latency.generation_tokens_per_second ?? 0 }}</b></div>
      </div>
      <div class="card"><h2>Duración de tareas</h2>
        <div v-if="!m.task_durations?.length" class="empty">Sin duraciones</div>
        <div v-for="t in m.task_durations" :key="t.id" class="metric">
          <small class="truncate" style="max-width:220px">{{ t.goal }}</small><b>{{ t.seconds }}s</b>
        </div>
      </div>
    </div>

    <div class="grid cols-3">
      <div class="card"><h2>Herramientas</h2>
        <div v-for="b in distTools" :key="b.k" class="bar-line"><span class="truncate">{{ b.k }}</span><i><b :style="{width:b.pct+'%'}"/></i><strong>{{ b.v }}</strong></div>
      </div>
      <div class="card"><h2>Tipos de tarea</h2>
        <div v-for="b in distTasks" :key="b.k" class="bar-line"><span class="truncate">{{ b.k }}</span><i><b :style="{width:b.pct+'%'}"/></i><strong>{{ b.v }}</strong></div>
      </div>
      <div class="card"><h2>Tipos de nodo</h2>
        <div v-for="b in distNodes" :key="b.k" class="bar-line"><span class="truncate">{{ b.k }}</span><i><b :style="{width:b.pct+'%'}"/></i><strong>{{ b.v }}</strong></div>
      </div>
    </div>
  </template>
</template>
