import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { errorsApi } from '@/api/errors.ts'
import { ApiError } from '@/api/client.ts'
import type { ErrorRecordsResponse } from '@/types/errors.ts'

export const useErrorsStore = defineStore('errors', () => {
  const result = ref<ErrorRecordsResponse | null>(null)
  const statusFilter = ref('')
  const categoryFilter = ref('')
  const loading = ref(false)
  const error = ref('')
  const lastUpdated = ref<Date | null>(null)
  const observedAt = ref(Date.now())
  const currentCursor = ref<string | null>(null)
  const previousCursors = ref<(string | null)[]>([])
  const displayedStatus = ref('')
  const displayedCategory = ref('')
  let sequence = 0
  let controller: AbortController | null = null

  const items = computed(() => {
    const cutoff = observedAt.value - (result.value?.retention.minutes ?? 10) * 60_000
    return result.value?.items.filter(record => Date.parse(record.time) >= cutoff) ?? []
  })
  const diagnosticItems = computed(() => {
    const diagnostics = result.value?.diagnostics
    const cutoff = observedAt.value - (diagnostics?.retention.minutes ?? 10) * 60_000
    return diagnostics?.items.filter(record => Date.parse(record.time) >= cutoff) ?? []
  })
  const page = computed(() => previousCursors.value.length + 1)
  const matchesFilters = computed(() => displayedStatus.value === statusFilter.value
    && displayedCategory.value === categoryFilter.value)

  async function fetchPage(cursor: string | null, trail: (string | null)[]): Promise<boolean> {
    const requestSequence = ++sequence
    observedAt.value = Date.now()
    controller?.abort()
    controller = new AbortController()
    const status = statusFilter.value
    const category = categoryFilter.value
    loading.value = true
    error.value = ''
    try {
      const response = await errorsApi.list({
        limit: 25,
        status: status ? Number(status) : undefined,
        category: category || undefined,
        cursor,
        include_diagnostics: true,
      }, controller.signal)
      if (requestSequence !== sequence) return false
      result.value = response
      observedAt.value = Date.now()
      currentCursor.value = cursor
      previousCursors.value = trail
      displayedStatus.value = status
      displayedCategory.value = category
      lastUpdated.value = new Date()
      return true
    } catch (err: unknown) {
      if (requestSequence !== sequence) return false
      error.value = err instanceof ApiError
        ? `读取错误记录失败（HTTP ${err.status}），请重试。`
        : '无法读取错误记录，请检查网络或服务状态后重试。'
      return false
    } finally {
      if (requestSequence === sequence) {
        loading.value = false
        controller = null
      }
    }
  }

  function refresh(): Promise<boolean> {
    return fetchPage(null, [])
  }

  function reload(): Promise<boolean> {
    return matchesFilters.value
      ? fetchPage(currentCursor.value, [...previousCursors.value])
      : refresh()
  }

  function nextPage(): Promise<boolean> {
    const cursor = result.value?.next_cursor
    if (loading.value || !matchesFilters.value || !result.value?.has_more || !cursor) return Promise.resolve(false)
    return fetchPage(cursor, [...previousCursors.value, currentCursor.value])
  }

  function previousPage(): Promise<boolean> {
    if (loading.value || !matchesFilters.value || previousCursors.value.length === 0) return Promise.resolve(false)
    const trail = [...previousCursors.value]
    const cursor = trail.pop() ?? null
    return fetchPage(cursor, trail)
  }

  function cancel() {
    ++sequence
    controller?.abort()
    controller = null
    loading.value = false
  }

  return {
    result, items, diagnosticItems, statusFilter, categoryFilter, loading, error, lastUpdated,
    page, matchesFilters, refresh, reload, nextPage, previousPage, cancel,
  }
})
