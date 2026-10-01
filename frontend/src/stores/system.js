import { defineStore } from 'pinia'
import { api } from '@/api/client'
import { REFRESH_INTERVAL_MS } from '@/utils/format'

export const useSystemStore = defineStore('system', {
  state: () => ({
    overview: null,
    error: null,
    online: false,
    toast: null,
    _timer: null,
  }),
  getters: {
    health: (state) => state.overview?.health ?? {},
    runtime: (state) => state.overview?.runtime ?? {},
    metrics: (state) => state.overview?.runtime?.metrics ?? {},
    idle: (state) => state.overview?.runtime?.idle ?? {},
    offpeak: (state) => state.overview?.runtime?.offpeak ?? {},
    taskCounts: (state) => state.overview?.task_counts ?? {},
    needsAttention: (state) => state.overview?.needs_attention ?? [],
    projects: (state) => state.overview?.projects ?? [],
  },
  actions: {
    notify(message, kind = 'info') {
      this.toast = { message, kind, id: Date.now() }
      window.setTimeout(() => {
        if (this.toast && Date.now() - this.toast.id >= 3400) this.toast = null
      }, 3500)
    },
    async refresh() {
      try {
        this.overview = await api.overview()
        this.online = true
        this.error = null
      } catch (error) {
        this.online = false
        this.error = error.message
      }
    },
    start(interval = REFRESH_INTERVAL_MS) {
      this.refresh()
      if (this._timer) return
      this._timer = window.setInterval(() => this.refresh(), interval)
    },
    stop() {
      if (this._timer) window.clearInterval(this._timer)
      this._timer = null
    },
  },
})
