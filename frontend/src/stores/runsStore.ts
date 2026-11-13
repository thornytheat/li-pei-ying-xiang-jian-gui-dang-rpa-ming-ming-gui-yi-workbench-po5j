import { defineStore } from 'pinia'
import {
  createRun,
  fetchReconcile,
  fetchRun,
  fetchRunItems,
  fetchRuns,
  resumeRun,
} from '../api/runs'
import type {
  ReconcileReport,
  RunItem,
  RunItemsResult,
  RunMode,
} from '../types/domain'

interface RunsState {
  items: RunItem[]
  selected: RunItem | null
  storedFiles: number
  loading: boolean
  starting: boolean
  error: string
  pollTimer: number | null
  checkpoint: RunItemsResult | null
  checkpointLoading: boolean
  reconcileReport: ReconcileReport | null
  reconcileLoading: boolean
}

export const useRunsStore = defineStore('runs', {
  state: (): RunsState => ({
    items: [],
    selected: null,
    storedFiles: 0,
    loading: false,
    starting: false,
    error: '',
    pollTimer: null,
    checkpoint: null,
    checkpointLoading: false,
    reconcileReport: null,
    reconcileLoading: false,
  }),
  getters: {
    latest: (state): RunItem | null => state.items[0] ?? null,
    busy: (state): boolean =>
      state.items.some((run) => run.status === 'queued' || run.status === 'running'),
  },
  actions: {
    async load() {
      this.loading = true
      this.error = ''
      try {
        const result = await fetchRuns()
        this.items = result.items
        this.storedFiles = result.stored_files
        if (!this.selected && this.items.length) this.selected = this.items[0]
        if (this.selected) {
          const fresh = this.items.find((run) => run.id === this.selected?.id)
          if (fresh) this.selected = fresh
        }
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '批次列表加载失败'
      } finally {
        this.loading = false
      }
    },
    select(run: RunItem | null) {
      this.selected = run
      this.checkpoint = null
    },
    async start(mode: RunMode, note: string) {
      this.starting = true
      this.error = ''
      try {
        const run = await createRun(mode, note)
        this.selected = run
        await this.load()
        if (run.status === 'queued' || run.status === 'running') this.poll(run.id)
        return run
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '批次发起失败'
        throw error
      } finally {
        this.starting = false
      }
    },
    async resume(runId: string, note = '') {
      this.starting = true
      this.error = ''
      try {
        const run = await resumeRun(runId, note)
        this.selected = run
        await this.load()
        this.poll(run.id)
        return run
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '续跑发起失败'
        throw error
      } finally {
        this.starting = false
      }
    },
    async loadCheckpoint(runId: string) {
      this.checkpointLoading = true
      try {
        this.checkpoint = await fetchRunItems(runId)
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '断点明细加载失败'
      } finally {
        this.checkpointLoading = false
      }
    },
    async loadReconcile() {
      this.reconcileLoading = true
      try {
        this.reconcileReport = await fetchReconcile()
      } catch (error) {
        this.error = (error as { message?: string }).message ?? '对账加载失败'
      } finally {
        this.reconcileLoading = false
      }
    },
    poll(runId: string) {
      if (this.pollTimer) window.clearTimeout(this.pollTimer)
      const tick = async () => {
        try {
          const run = await fetchRun(runId)
          this.selected = run
          this.items = this.items.map((item) => (item.id === run.id ? run : item))
          if (run.status === 'queued' || run.status === 'running') {
            this.pollTimer = window.setTimeout(tick, 1200)
          } else {
            this.pollTimer = null
            await this.load()
            if (this.selected?.id === runId) await this.loadCheckpoint(runId)
          }
        } catch {
          this.pollTimer = null
        }
      }
      this.pollTimer = window.setTimeout(tick, 900)
    },
    stopPoll() {
      if (this.pollTimer) window.clearTimeout(this.pollTimer)
      this.pollTimer = null
    },
  },
})
