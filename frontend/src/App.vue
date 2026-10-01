<script setup>
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useSystemStore } from '@/stores/system'
import faviconUrl from '@/../public/favicon.svg'

const system = useSystemStore()
const route = useRoute()

const title = computed(() => route.meta?.title || 'Assistant')
const nav = [
  { to: '/', icon: '◎', label: 'Resumen' },
  { to: '/tasks', icon: '≣', label: 'Tareas' },
  { to: '/schedules', icon: '⏱', label: 'Horarios' },
  { to: '/observability', icon: '♡', label: 'Observabilidad' },
  { to: '/metrics', icon: '∑', label: 'Métricas' },
  { to: '/resources', icon: '⌁', label: 'Recursos' },
  { to: '/projects', icon: '▣', label: 'Proyectos' },
  { to: '/memory', icon: '◈', label: 'Memoria' },
  { to: '/chat', icon: '✦', label: 'Chat' },
]

onMounted(() => system.start())
onUnmounted(() => system.stop())
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <img class="brand-mark" :src="faviconUrl" alt="" />
        <div>
          <div class="brand-name">Assistant</div>
          <div class="brand-sub">Control Room</div>
        </div>
      </div>
      <nav class="nav">
        <RouterLink v-for="item in nav" :key="item.to" :to="item.to" class="nav-item">
          <span class="ico">{{ item.icon }}</span>
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>
      <div class="sidebar-foot">
        <div>{{ system.health.status || '—' }} · {{ system.health.llm_ready ? 'LLM listo' : 'LLM no disponible' }}</div>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <h1>{{ title }}</h1>
        <span class="spacer" />
        <span class="pill" :class="system.online ? 'ok' : 'error'">
          <span class="dot" />{{ system.online ? 'Conectado' : 'Offline' }}
        </span>
        <span v-if="system.runtime.active_tasks" class="pill run">
          <span class="dot" />{{ system.runtime.active_tasks }} activas
        </span>
        <button class="btn ghost" @click="system.refresh()">Actualizar</button>
      </header>

      <main class="content">
        <RouterView />
      </main>
    </div>

    <Transition name="fade">
      <div v-if="system.toast" class="toast" :class="system.toast.kind">
        {{ system.toast.message }}
      </div>
    </Transition>
  </div>
</template>
