import { onUnmounted, ref, shallowRef, watch } from 'vue'
import { request } from '@/api/client'
import { usePolling } from './usePolling'

export function useConsoleFeed<T>(endpoint: string) {
  const items = shallowRef<T[]>([])
  const error = ref('')
  const loading = ref(false)
  const live = ref(true)
  const updated = ref('')
  let disposed = false
  let controller: AbortController | undefined

  async function refresh() {
    if (loading.value || disposed) return
    loading.value = true
    controller = new AbortController()
    try {
      const result = await request<{ items: T[] }>(endpoint, { signal: controller.signal })
      if (disposed) return
      items.value = result.items
      error.value = ''
      updated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    } catch (cause) {
      if (!disposed && !controller.signal.aborted) error.value = cause instanceof Error ? cause.message : '暂时无法读取数据'
    } finally {
      if (!disposed) loading.value = false
    }
  }
  const polling = usePolling(refresh, 2000)
  watch(live, (value) => {
    if (value) { polling.resume(); void refresh() }
    else polling.pause()
  })
  onUnmounted(() => { disposed = true; controller?.abort() })
  return { items, error, loading, live, updated, refresh }
}
