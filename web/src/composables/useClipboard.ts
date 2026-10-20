import { onUnmounted, ref } from 'vue'
import { useToastStore } from '@/stores/toast.ts'

/**
 * 复制文本到剪贴板，并在 2 秒内记录“已复制”的键；
 * 同一键重复触发时重置计时。
 */
export function useClipboard() {
  const toast = useToastStore()
  const copied = ref<string | number | null>(null)
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  let sequence = 0

  onUnmounted(() => {
    disposed = true
    sequence++
    clearTimeout(timer)
    copied.value = null
  })

  async function copy(text: string, key: string | number, message = '已复制到剪贴板') {
    if (disposed) return
    const request = ++sequence
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      if (!disposed && request === sequence) {
        toast.error('复制失败，浏览器可能拒绝了剪贴板权限')
      }
      return
    }
    if (disposed || request !== sequence) return
    copied.value = key
    toast.success(message)
    clearTimeout(timer)
    timer = setTimeout(() => {
      if (copied.value === key) copied.value = null
    }, 2000)
  }

  return { copied, copy }
}
