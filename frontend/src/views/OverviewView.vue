<script setup>
import { computed } from 'vue'
import { useSystemStore } from '@/stores/system'
import MetricCard from '@/components/MetricCard.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { orderStatuses, formatNumber } from '@/utils/format'

const system = useSystemStore()
const counts = computed(() => system.taskCounts)
const ordered = computed(() => orderStatuses(counts.value))
const maxCount = computed(() => Math.max(1, ...ordered.value.map(([, c]) => c)))
const active = computed(() => system.runtime.active_tasks ?? 0)
const idle = computed(() => system.idle ?? {})
const metrics = computed(() => system.metrics ?? {})
</script>

<template>
  <div v-if="system.error" class="card" style="border-color:var(--error)">
    <strong style="color:var(--error)">Sin conexión con la API</strong>
    <p class="muted">{{ system.error }}<br />¿Está en marcha <code>assistant run --dashboard</code>?</p>
  </div>

  <template v-else>
    <div class="grid cols-4" style="margin-bottom:16px">
      <MetricCard label="Tareas activas" :value="active" hint="Ejecutándose ahora" />
      <MetricCard label="Total tareas" :value="Object.values(counts).reduce((a, b) => a + b, 0)" />
      <MetricCard label="Proyectos" :value="system.projects.length" />
      <MetricCard label="Idle cycle" :value="idle.enabled ? 'ON' : 'OFF'" :hint="idle.maintenance_runs ? idle.maintenance_runs + ' mantenimientos' : 'inactivo'" />
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h2>Distribución de tareas</h2>
        <div v-if="!ordered.length" class="empty">Sin datos todavía</div>
        <div v-for="[status, count] in ordered" :key="status" class="bar-line">
          <span>{{ status }}</span>
          <i><b :style="{ width: (count / maxCount) * 100 + '%' }" /></i>
          <strong>{{ count }}</strong>
        </div>
      </div>

      <div class="card">
        <h2>Estado del runtime</h2>
        <div class="metric"><small>Modo LLM</small><b>{{ system.health.llm_ready ? 'Listo' : 'No disponible' }}</b></div>
        <div class="metric"><small>Base de datos</small><b>{{ system.health.database_ready ? 'OK' : 'Error' }}</b></div>
        <div class="metric"><small>Tareas sin terminar</small><b>{{ formatNumber(system.health.unfinished_tasks) }}</b></div>
        <div class="metric"><small>Métricas runtime</small><b>{{ Object.keys(metrics).length }} claves</b></div>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h2>Proyectos habilitados</h2>
      <div v-if="!system.projects.length" class="empty">Sin proyectos registrados</div>
      <div>
        <RouterLink v-for="p in system.projects" :key="p.id" :to="{ name: 'projects' }">
          <span class="chip">{{ p.name }} <span v-if="!p.enabled" class="muted">(off)</span></span>
        </RouterLink>
      </div>
    </div>
  </template>
</template>
