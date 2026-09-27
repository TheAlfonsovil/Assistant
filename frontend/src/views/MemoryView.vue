<script setup>
import { onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import { date, prettyJson } from '@/utils/format'

const system = useSystemStore()
const items = ref([])
const summary = ref(null)
const open = ref(null)

async function load() {
  try {
    const [memories, tables] = await Promise.all([api.listMemory(), api.memorySummary()])
    items.value = memories
    summary.value = tables
  } catch (error) { system.notify(error.message, 'error') }
}
async function redact(m) { try { await api.redactMemory(m.id); system.notify('Memoria redactada', 'ok'); await load() } catch (e) { system.notify(e.message, 'error') } }
async function remove(m) { try { await api.deleteMemory(m.id); system.notify('Memoria eliminada', 'ok'); await load() } catch (e) { system.notify(e.message, 'error') } }
async function purge() { try { const r = await api.purgeExpired(); system.notify(`${r.purged} caducadas`, 'ok'); await load() } catch (e) { system.notify(e.message, 'error') } }
async function exportAll() {
  const data = await api.exportMemory()
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob); const a = document.createElement('a')
  a.href = url; a.download = 'assistant-memory.json'; a.click(); URL.revokeObjectURL(url)
}
function value(m) { return typeof m.value === 'object' ? prettyJson(m.value) : String(m.value) }
onMounted(load)
</script>

<template>
  <div class="row" style="margin-bottom:14px">
    <button class="btn ghost" @click="load">Actualizar</button>
    <button class="btn" @click="exportAll">Exportar</button>
    <button class="btn" @click="purge">Purgar caducadas</button>
    <span class="grow" /><span class="muted">{{ items.length }} registros</span>
  </div>

  <div class="card" style="margin-bottom:16px">
    <div class="row">
      <h2 style="margin:0">Tablas SQLite</h2>
      <span class="grow" />
      <span class="muted">{{ summary?.total_rows ?? 0 }} filas en total</span>
    </div>
    <div v-if="!summary" class="empty">Cargando resumen…</div>
    <div v-else class="grid cols-4" style="margin-top:12px">
      <div v-for="t in summary.tables" :key="t.name" class="metric">
        <small class="mono">{{ t.name }}</small><b>{{ t.rows }}</b>
      </div>
    </div>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <table class="table">
      <thead><tr><th>Tipo</th><th>Clave</th><th>Valor</th><th>Alcance</th><th>Fuente</th><th>Uso</th><th></th></tr></thead>
      <tbody>
        <tr v-for="m in items" :key="m.id" @click="open = open === m.id ? null : m.id">
          <td><span class="chip">{{ m.kind }}</span></td>
          <td class="mono">{{ m.key }}</td>
          <td class="truncate mono" style="font-size:12px">{{ open === m.id ? value(m) : value(m).slice(0, 60) }}</td>
          <td class="muted">{{ m.scope }}<span v-if="m.scope_id">:{{ m.scope_id.slice(0,6) }}</span></td>
          <td class="muted">{{ m.source }}</td>
          <td>{{ m.usage_count }}</td>
          <td>
            <div class="row" @click.stop>
              <button class="btn ghost" @click="redact(m)">Redactar</button>
              <button class="btn danger" @click="remove(m)">Borrar</button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-if="!items.length" class="empty">Sin memoria registrada.</div>
  </div>
</template>
