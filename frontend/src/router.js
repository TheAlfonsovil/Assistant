import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', name: 'overview', component: () => import('@/views/OverviewView.vue'), meta: { title: 'Resumen operativo' } },
  { path: '/tasks', name: 'tasks', component: () => import('@/views/TasksView.vue'), meta: { title: 'Tareas persistentes' } },
  { path: '/tasks/:id', name: 'task-detail', component: () => import('@/views/TaskDetailView.vue'), meta: { title: 'Detalle de tarea' } },
  { path: '/observability', name: 'observability', component: () => import('@/views/ObservabilityView.vue'), meta: { title: 'Observabilidad' } },
  { path: '/metrics', name: 'metrics', component: () => import('@/views/MetricsView.vue'), meta: { title: 'Métricas' } },
  { path: '/resources', name: 'resources', component: () => import('@/views/ResourcesView.vue'), meta: { title: 'Recursos conectados' } },
  { path: '/projects', name: 'projects', component: () => import('@/views/ProjectsView.vue'), meta: { title: 'Proyectos' } },
  { path: '/memory', name: 'memory', component: () => import('@/views/MemoryView.vue'), meta: { title: 'Memoria' } },
  { path: '/chat', name: 'chat', component: () => import('@/views/ChatView.vue'), meta: { title: 'Chat rápido' } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory('/dashboard/'),
  routes,
})

router.afterEach((to) => {
  document.title = to.meta?.title ? `${to.meta.title} · Assistant` : 'Assistant · Control Room'
})

export default router
