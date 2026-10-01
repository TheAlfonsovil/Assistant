<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import MetricCard from '@/components/MetricCard.vue'
import { formatNumber, formatSeconds } from '@/utils/format'

const system = useSystemStore()
const m = ref(null)
const series = ref([])
async function load() {
  try {
    m.value = await api.metrics(500)
    series.value = (await api.metricsSeries(24)).buckets ?? []
  } catch (e) { system.notify(e.message, 'error') }
}
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
const actual = computed(() => m.value?.actual_tokens ?? {})
const cache = computed(() => m.value?.cache ?? {})
const reliability = computed(() => m.value?.reliability ?? {})
const queue = computed(() => m.value?.queue ?? {})
const cost = computed(() => m.value?.cost ?? {})
const workerRows = computed(() =>
  Object.entries(m.value?.worker_outcomes ?? {})
    .map(([worker, stats]) => ({ worker, ...stats }))
    .sort((a, b) => b.total - a.total),
)
const blockerRows = computed(() => Object.entries(reliability.value.blockers ?? {}))
const costText = computed(() => {
  if (!cost.value.configured) return 'no configurado'
  return `${cost.value.total_usd} ${cost.value.currency}`
})
// Only hours with activity: 24 rows of zeros hide the interesting buckets.
const activeHours = computed(() =>
  series.value.filter((bucket) => bucket.llm_calls || bucket.tool_calls || bucket.tasks_created),
)
function hourLabel(iso) {
  const moment = new Date(iso)
  const day = `${moment.getUTCDate()}`.padStart(2, '0')
  const hour = `${moment.getUTCHours()}`.padStart(2, '0')
  return `${day}/${moment.getUTCMonth() + 1} ${hour}:00`
}
</script>

<template>
  <div v-if="!m" class="empty">Cargando métricas…</div>
  <template v-else>
    <div class="row" style="margin-bottom:14px">
      <span class="chip">modelo: {{ m.model_label || m.model }}</span>
      <span class="chip">tokens: {{ tokens.source }}</span>
      <span class="chip">coste: {{ costText }}</span>
      <span class="grow" /><button class="btn ghost" @click="load">Actualizar</button>
    </div>

    <div class="grid cols-4" style="margin-bottom:16px">
      <MetricCard label="Tokens prompt" :value="formatNumber(tokens.prompt)" />
      <MetricCard label="Tokens respuesta" :value="formatNumber(tokens.response)" />
      <MetricCard label="Éxito" :value="throughput.success_rate + '%'" :hint="throughput.completed_tasks + '/' + throughput.terminal_tasks + ' terminales'" />
      <MetricCard label="Reintentos" :value="throughput.retries ?? 0" />
    </div>

    <div class="grid cols-4" style="margin-bottom:16px">
      <MetricCard label="Tokens medidos" :value="formatNumber(actual.total ?? 0)" :hint="actual.available ? 'proveedor' : 'estimado'" />
      <MetricCard label="Acierto de caché" :value="(actual.cache_hit_rate ?? 0) + '%'" :hint="formatNumber(actual.cached ?? 0) + ' en caché'" />
      <MetricCard label="Tokens de razonamiento" :value="formatNumber(actual.reasoning ?? 0)" />
      <MetricCard label="Coste" :value="costText" :hint="cost.configured ? '' : cost.reason" />
    </div>

    <div class="grid cols-3" style="margin-bottom:16px">
      <div class="card"><h2>Caché de contexto</h2>
        <div class="metric"><small>Acierto</small><b>{{ (cache.hit_rate ?? 0) }}%</b></div>
        <div class="metric"><small>Tokens en caché</small><b>{{ formatNumber(cache.cached_tokens ?? 0) }}</b></div>
        <div class="metric"><small>Tokens sin caché</small><b>{{ formatNumber(cache.miss_tokens ?? 0) }}</b></div>
        <div v-if="cache.saving_usd !== null && cache.saving_usd !== undefined" class="metric">
          <small>Ahorro por caché</small><b>{{ cache.saving_usd }} {{ cache.currency }}</b>
        </div>
        <div class="muted" style="font-size:11px;margin-top:8px">{{ cache.note }}</div>
      </div>
      <div class="card"><h2>Cola</h2>
        <div class="metric"><small>En cola</small><b>{{ queue.queued ?? 0 }}</b></div>
        <div class="metric"><small>En ejecución</small><b>{{ queue.running ?? 0 }}</b></div>
        <div class="metric"><small>Esperando usuario</small><b>{{ queue.waiting ?? 0 }}</b></div>
        <div class="metric"><small>Bloqueadas</small><b>{{ queue.blocked ?? 0 }}</b></div>
        <div class="metric"><small>Espera media</small><b>{{ formatSeconds(queue.average_queue_seconds) }}</b></div>
        <div class="metric"><small>Espera máxima</small><b>{{ formatSeconds(queue.max_queue_seconds) }}</b></div>
      </div>
      <div class="card"><h2>Fiabilidad</h2>
        <div class="metric"><small>Llamadas a herramientas</small><b>{{ reliability.tool_calls ?? 0 }}</b></div>
        <div class="metric"><small>Fallos de herramienta</small><b>{{ reliability.tool_failures ?? 0 }}</b></div>
        <div class="metric"><small>Éxito de herramienta</small><b>{{ reliability.tool_success_rate ?? 0 }}%</b></div>
        <div class="metric"><small>Reintentos programados</small><b>{{ reliability.retries ?? 0 }}</b></div>
      </div>
      <div class="card"><h2>Motivos de bloqueo</h2>        <div v-if="!blockerRows.length" class="empty">Sin bloqueos registrados</div>
        <div v-for="[reason, count] in blockerRows" :key="reason" class="metric">
          <small class="truncate" style="max-width:240px">{{ reason }}</small><b>{{ count }}</b>
        </div>
      </div>
    </div>

    <div class="grid cols-2" style="margin-bottom:16px">
      <div class="card"><h2>Resultado por worker</h2>
        <div v-if="!workerRows.length" class="empty">Sin tareas enroutadas</div>
        <div v-for="w in workerRows" :key="w.worker" class="metric">
          <small class="truncate" style="max-width:220px">{{ w.worker }}</small>
          <b>{{ w.succeeded }}✓ / {{ w.failed }}✗ / {{ w.blocked }}⛔ · {{ w.total }}</b>
        </div>
      </div>
      <div class="card"><h2>Percentiles de latencia</h2>
        <div class="metric"><small>LLM p50 / p95</small><b>{{ latency.llm_p50_seconds ?? 0 }}s / {{ latency.llm_p95_seconds ?? 0 }}s</b></div>
        <div class="metric"><small>Herramienta p50 / p95</small><b>{{ latency.tool_p50_seconds ?? 0 }}s / {{ latency.tool_p95_seconds ?? 0 }}s</b></div>
        <div class="metric"><small>Tarea p50 / p95</small><b>{{ latency.task_p50_seconds ?? 0 }}s / {{ latency.task_p95_seconds ?? 0 }}s</b></div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px"><h2>Últimas 24 h (UTC)</h2>
      <div v-if="!activeHours.length" class="empty">Sin actividad registrada en la ventana retenida</div>
      <div v-for="b in activeHours" :key="b.hour" class="metric">
        <small>{{ hourLabel(b.hour) }}</small>
        <b>
          {{ b.llm_calls }} LLM · {{ formatNumber(b.prompt_tokens + b.completion_tokens) }} tok
          · {{ b.tool_calls }} tools<template v-if="b.tool_failures"> ({{ b.tool_failures }} ✗)</template>
          · {{ b.tasks_finished }} fin.<template v-if="b.average_latency_ms"> · {{ b.average_latency_ms }} ms</template>
        </b>
      </div>
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
