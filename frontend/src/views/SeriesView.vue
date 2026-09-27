<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import { date } from '@/utils/format'

const system = useSystemStore()

// The three dashboard options.
const TABS = [
  { id: 'character', label: 'Crear personaje' },
  { id: 'scene', label: 'Crear escena' },
  { id: 'results', label: 'Visualizar resultados' },
]
const tab = ref('character')

const status = ref(null)
const characters = ref([])
const scenes = ref([])
const renders = ref([])
const busy = ref(false)

const character = ref({ name: '', description: '', palette: '', style_notes: '' })
const scene = ref({ name: '', character_id: '' })

// A scene is data, not code: duration, background, camera and elements. The
// template is a shape that actually renders, so the first scene is a working
// starting point rather than an empty box.
const SPEC_TEMPLATE = [
  '{',
  '  "duration": 2,',
  '  "background": "#05070d",',
  '  "camera": { "x": 960, "y": 540, "zoom": 1 },',
  '  "elements": [',
  '    { "type": "text", "text": "TITULO", "start": 0, "duration": 2, "y": 860, "size": 120, "fill": "#e2f4ff" }',
  '  ]',
  '}',
].join('\n')

const sceneDraft = ref(SPEC_TEMPLATE)
const sceneIssues = ref([])

const charactersById = computed(() =>
  Object.fromEntries(characters.value.map((c) => [c.id, c.name])),
)

function parseSpec() {
  try {
    return { value: JSON.parse(sceneDraft.value || '{}'), error: null }
  } catch (error) {
    return { value: null, error: `JSON invalido: ${error.message}` }
  }
}

async function load() {
  busy.value = true
  try {
    const [s, c, sc, r] = await Promise.all([
      api.seriesStatus(),
      api.listCharacters(),
      api.listScenes(),
      api.listRenders(),
    ])
    status.value = s
    characters.value = c
    scenes.value = sc
    renders.value = r
  } catch (error) {
    system.notify(error.message, 'error')
  } finally {
    busy.value = false
  }
}

async function createCharacter() {
  if (!character.value.name.trim()) {
    system.notify('El nombre es obligatorio', 'error')
    return
  }
  // Palette is typed as `key:value` pairs; the API takes an object.
  const palette = Object.fromEntries(
    character.value.palette
      .split(/[\n,]/)
      .map((line) => line.split(':').map((part) => part.trim()))
      .filter(([key, value]) => key && value),
  )
  try {
    await api.createCharacter({
      name: character.value.name.trim(),
      description: character.value.description,
      palette,
      style_notes: character.value.style_notes,
    })
    system.notify('Personaje creado', 'ok')
    character.value = { name: '', description: '', palette: '', style_notes: '' }
    await load()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

async function removeCharacter(item) {
  if (!confirm(`Eliminar el personaje "${item.name}"? Sus escenas se conservan.`)) return
  try {
    await api.deleteCharacter(item.id)
    system.notify('Personaje eliminado', 'ok')
    await load()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

async function createScene() {
  if (!scene.value.name.trim()) {
    system.notify('El nombre es obligatorio', 'error')
    return
  }
  const parsed = parseSpec()
  if (parsed.error) {
    system.notify(parsed.error, 'error')
    return
  }
  try {
    const created = await api.createScene({
      name: scene.value.name.trim(),
      character_id: scene.value.character_id || null,
      spec: parsed.value,
    })
    sceneIssues.value = created.issues || []
    const blocking = sceneIssues.value.filter((i) => i.level === 'error')
    system.notify(
      blocking.length
        ? `Escena guardada con ${blocking.length} error(es) de contrato`
        : 'Escena creada',
      blocking.length ? 'error' : 'ok',
    )
    scene.value = { name: '', character_id: '' }
    await load()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

async function removeScene(item) {
  if (!confirm(`Eliminar la escena "${item.name}"?`)) return
  try {
    await api.deleteScene(item.id)
    system.notify('Escena eliminada', 'ok')
    await load()
  } catch (error) {
    system.notify(error.message, 'error')
  }
}

function videoUrl(render) {
  return render.video_path ? api.seriesVideo(render.id) : ''
}

onMounted(load)
</script>

<template>
  <div class="row" style="margin-bottom:14px">
    <button
      v-for="t in TABS"
      :key="t.id"
      class="btn"
      :class="tab === t.id ? 'primary' : 'ghost'"
      @click="tab = t.id"
    >{{ t.label }}</button>
    <span class="grow" />
    <button class="btn ghost" :disabled="busy" @click="load">Actualizar</button>
  </div>

  <div
    v-if="status && !status.enabled"
    class="card"
    style="margin-bottom:16px;border-color:#b45309"
  >
    <strong>Series esta desactivado.</strong>
    <div class="muted" style="margin-top:6px">
      El render sigue funcionando desde <span class="mono">series/</span>, pero la API no
      lo lanzara. Activalo con <span class="mono">ASSISTANT_SERIES_ENABLED=true</span>.
    </div>
    <div class="muted mono" style="font-size:11px;margin-top:8px">
      {{ status.width }}x{{ status.height }} - {{ status.fps }} fps - {{ status.format }} -
      raiz {{ status.root }} {{ status.root_exists ? 'ok' : 'FALTA' }}
    </div>
  </div>

  <!-- 1. Crear personaje -->
  <template v-if="tab === 'character'">
    <div class="card" style="margin-bottom:16px">
      <h2>Nuevo personaje</h2>
      <div class="muted" style="margin-bottom:12px">
        Identidad visual reutilizable. La paleta y las notas de estilo se comparten con
        todas sus escenas para que el personaje sea reconocible entre planos.
      </div>
      <div class="grid cols-2">
        <input v-model="character.name" class="input" placeholder="Nombre" />
        <input v-model="character.style_notes" class="input" placeholder="Notas de estilo (opcional)" />
      </div>
      <div style="margin-top:10px">
        <input v-model="character.description" class="input" placeholder="Descripcion (opcional)" />
      </div>
      <div style="margin-top:10px">
        <input
          v-model="character.palette"
          class="input mono"
          placeholder="Paleta: primary:#38bdf8, accent:#f59e0b"
        />
      </div>
      <div class="row" style="margin-top:12px">
        <span class="grow" />
        <button class="btn primary" @click="createCharacter">Crear personaje</button>
      </div>
    </div>

    <div class="section-title">Personajes ({{ characters.length }})</div>
    <div class="grid cols-2">
      <div v-for="c in characters" :key="c.id" class="card">
        <div class="row">
          <h3 class="grow" style="margin:0">{{ c.name }}</h3>
          <button class="btn danger" @click="removeCharacter(c)">Eliminar</button>
        </div>
        <div v-if="c.description" class="muted" style="margin:8px 0">{{ c.description }}</div>
        <div v-if="c.style_notes" class="muted" style="font-size:12px">Estilo: {{ c.style_notes }}</div>
        <div style="margin-top:8px">
          <span v-for="(value, key) in c.palette" :key="key" class="chip">
            <span class="dot" :style="`background:${value}`" />{{ key }} {{ value }}
          </span>
          <span v-if="!Object.keys(c.palette || {}).length" class="muted">Sin paleta</span>
        </div>
        <div class="muted" style="font-size:11px;margin-top:8px">
          {{ scenes.filter((s) => s.character_id === c.id).length }} escena(s)
        </div>
      </div>
    </div>
    <div v-if="!characters.length" class="empty">Todavia no hay personajes.</div>
  </template>

  <!-- 2. Crear escena -->
  <template v-else-if="tab === 'scene'">
    <div class="card" style="margin-bottom:16px">
      <h2>Nueva escena</h2>
      <div class="muted" style="margin-bottom:12px">
        Una escena es <strong>datos</strong>, no codigo: duracion, fondo, camara y elementos.
        Se valida al guardar y se puede volver a renderizar sin volver a llamar al modelo.
      </div>
      <div class="grid cols-2">
        <input v-model="scene.name" class="input" placeholder="Nombre de la escena" />
        <select v-model="scene.character_id" class="select">
          <option value="">Sin personaje</option>
          <option v-for="c in characters" :key="c.id" :value="c.id">{{ c.name }}</option>
        </select>
      </div>
      <div style="margin-top:10px">
        <textarea v-model="sceneDraft" class="input mono" rows="15" spellcheck="false" />
      </div>
      <div v-if="sceneIssues.length" style="margin-top:10px">
        <div
          v-for="(issue, i) in sceneIssues"
          :key="i"
          class="chip"
          :style="issue.level === 'error' ? 'border-color:#ef4444;color:#fca5a5' : 'border-color:#f59e0b;color:#fcd34d'"
        >{{ issue.level }} - {{ issue.path }}: {{ issue.message }}</div>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="btn ghost" @click="sceneDraft = SPEC_TEMPLATE">Restaurar plantilla</button>
        <span class="grow" />
        <button class="btn primary" @click="createScene">Crear escena</button>
      </div>
    </div>

    <div class="section-title">Escenas ({{ scenes.length }})</div>
    <div class="grid cols-2">
      <div v-for="s in scenes" :key="s.id" class="card">
        <div class="row">
          <h3 class="grow" style="margin:0">{{ s.name }}</h3>
          <button class="btn danger" @click="removeScene(s)">Eliminar</button>
        </div>
        <div class="row" style="margin:8px 0">
          <span class="chip">{{ s.duration }}s</span>
          <span class="chip">{{ (s.spec && s.spec.elements ? s.spec.elements.length : 0) }} elementos</span>
          <span v-if="s.character_id" class="chip">{{ charactersById[s.character_id] || 'personaje' }}</span>
        </div>
        <div v-if="(s.issues || []).length">
          <div
            v-for="(issue, i) in s.issues"
            :key="i"
            class="muted"
            style="font-size:11px"
            :style="issue.level === 'error' ? 'color:#fca5a5' : 'color:#fcd34d'"
          >{{ issue.level }} - {{ issue.path }}: {{ issue.message }}</div>
        </div>
        <div v-else class="muted" style="font-size:11px">Sin incidencias.</div>
        <div class="muted mono" style="font-size:11px;margin-top:8px">{{ date(s.updated_at) }}</div>
      </div>
    </div>
    <div v-if="!scenes.length" class="empty">Todavia no hay escenas.</div>
  </template>

  <!-- 3. Visualizar resultados -->
  <template v-else>
    <div class="section-title">Renderizados ({{ renders.length }})</div>
    <div v-for="r in renders" :key="r.id" class="card" style="margin-bottom:12px">
      <div class="row">
        <h3 class="grow" style="margin:0">
          {{ (scenes.find((s) => s.id === r.scene_id) || {}).name || r.scene_id }}
        </h3>
        <span
          class="pill"
          :class="r.status === 'COMPLETED' ? 'ok' : r.status === 'FAILED' ? 'error' : 'muted'"
        ><span class="dot" />{{ r.status }}</span>
      </div>
      <div class="row" style="margin:8px 0">
        <span class="chip">{{ r.format }}</span>
        <span v-if="r.duration" class="chip">{{ r.duration }}s</span>
        <span class="chip">{{ date(r.created_at) }}</span>
      </div>
      <video
        v-if="r.video_path"
        :src="videoUrl(r)"
        controls
        preload="metadata"
        style="width:100%;max-width:520px;border-radius:10px;background:#000"
      />
      <div v-else class="muted" style="font-size:12px">Sin fichero de video registrado.</div>
      <div v-if="r.error" class="muted" style="color:#fca5a5;font-size:12px;margin-top:8px">
        {{ r.error }}
      </div>
    </div>
    <div v-if="!renders.length" class="empty">
      Todavia no hay renderizados. Exporta desde <span class="mono">series/</span>
      (<span class="mono">npm run export</span>) y registra el resultado.
    </div>
  </template>
</template>
