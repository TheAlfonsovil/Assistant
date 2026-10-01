<script setup>
import { onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import { date } from '@/utils/format'

const system = useSystemStore()
const projects = ref([])
const showForm = ref(false)
const form = ref({ name: '', path: '', description: '', project_type: 'code', enabled: true })

async function load() {
  try { projects.value = await api.listProjects() }
  catch (error) { system.notify(error.message, 'error') }
}
async function create() {
  if (!form.value.name.trim() || !form.value.path.trim()) {
    system.notify('Nombre y ruta son obligatorios', 'error'); return
  }
  try {
    await api.createProject({ ...form.value, name: form.value.name.trim(), path: form.value.path.trim() })
    system.notify('Proyecto creado', 'ok'); showForm.value = false
    form.value = { name: '', path: '', description: '', project_type: 'code', enabled: true }
    await load()
  } catch (error) { system.notify(error.message, 'error') }
}
async function audit(p, runTests) {
  try { await api.auditProject(p.id, runTests); system.notify('Auditoria lanzada', 'ok') }
  catch (error) { system.notify(error.message, 'error') }
}
async function refresh(p) {
  try { await api.refreshCodegraph(p.id); system.notify('Codegraph actualizado', 'ok'); await load() }
  catch (error) { system.notify(error.message, 'error') }
}
async function remove(p) {
  if (!confirm(`¿Eliminar ${p.name}?`)) return
  try { await api.deleteProject(p.id); system.notify('Proyecto eliminado', 'ok'); await load() }
  catch (error) { system.notify(error.message, 'error') }
}
onMounted(load)
</script>

<template>
  <div class="row" style="margin-bottom:14px">
    <button class="btn primary" @click="showForm = !showForm">＋ Nuevo proyecto</button>
    <span class="grow" /><button class="btn ghost" @click="load">Actualizar</button>
  </div>

  <div v-if="showForm" class="card" style="margin-bottom:16px">
    <h2>Registrar proyecto</h2>
    <div class="grid cols-2">
      <input v-model="form.name" class="input" placeholder="Nombre" />
      <input v-model="form.path" class="input" placeholder="Ruta absoluta" />
      <input v-model="form.description" class="input" placeholder="Descripción (opcional)" />
      <select v-model="form.project_type" class="select">
        <option value="code">code</option><option value="workspace">workspace</option>
      </select>
    </div>
    <div class="row" style="margin-top:12px">
      <label class="row"><input type="checkbox" v-model="form.enabled" /> habilitado</label>
      <span class="grow" /><button class="btn primary" @click="create">Guardar</button>
    </div>
  </div>

  <div class="grid cols-2">
    <div v-for="p in projects" :key="p.id" class="card">
      <div class="row">
        <h3 class="grow" style="margin:0">{{ p.name }}</h3>
        <span class="pill" :class="p.enabled ? 'ok' : 'muted'"><span class="dot" />{{ p.enabled ? 'activo' : 'off' }}</span>
      </div>
      <div class="mono muted" style="font-size:12px;margin:8px 0">{{ p.path }}</div>
      <div v-if="p.description" class="muted" style="margin-bottom:8px">{{ p.description }}</div>
      <div>
        <span class="chip">{{ p.project_type }}</span>
        <span v-if="p.codegraph" class="chip">
          {{ p.codegraph.module_count || 0 }} mód · {{ p.codegraph.edge_count || 0 }} aristas
        </span>
        <span v-if="p.codegraph?.partial" class="chip warn" title="El índice se recortó por los topes configurados">
          ⚠ índice parcial
        </span>
        <span v-if="p.is_default" class="chip">default</span>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="btn" @click="audit(p, false)">Auditar</button>
        <button class="btn" @click="audit(p, true)">Auditar + tests</button>
        <button class="btn" @click="refresh(p)">Codegraph</button>
        <span class="grow" />
        <button class="btn danger" @click="remove(p)">Eliminar</button>
      </div>
      <div class="muted" style="font-size:11px;margin-top:8px">
        Índice: {{ p.codegraph_updated_at ? date(p.codegraph_updated_at) : 'sin construir' }}
        · Auditoría: {{ date(p.last_audited_at) }}
      </div>
    </div>
  </div>
  <div v-if="!projects.length" class="empty">Sin proyectos.</div>
</template>
