<script setup lang="ts">
import { watch, ref, nextTick, onMounted, onUnmounted } from 'vue'
import { X } from 'lucide-vue-next'
import { isTopModal, setModalOpen } from './modalState.ts'

interface Props {
  modelValue: boolean
  title?: string
  maxWidth?: 'sm' | 'md' | 'lg' | 'xl' | '2xl'
}

const props = withDefaults(defineProps<Props>(), {
  title: '',
  maxWidth: 'md',
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  close: []
}>()

const owner = Symbol('modal')
const panel = ref<HTMLElement | null>(null)
let previousFocus: HTMLElement | null = null

function syncOpen(open: boolean) {
  if (typeof document === 'undefined') return
  const wasTop = isTopModal(owner)
  if (open && !wasTop && document.activeElement instanceof HTMLElement) {
    previousFocus = document.activeElement
  }
  setModalOpen(owner, open)
  if (open) {
    void nextTick(() => {
      if (props.modelValue && isTopModal(owner)) panel.value?.focus()
    })
  } else if (wasTop && previousFocus?.isConnected) {
    previousFocus.focus()
  }
}

function closeModal() {
  emit('update:modelValue', false)
  emit('close')
}

function handleKeydown(e: KeyboardEvent) {
  if (!props.modelValue || !isTopModal(owner)) return
  if (e.key === 'Escape') {
    e.preventDefault()
    closeModal()
  } else if (e.key === 'Tab' && panel.value) {
    const items = [...panel.value.querySelectorAll<HTMLElement>(
      'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )].filter(item => item.getClientRects().length > 0)
    const first = items[0], last = items.at(-1)
    const active = document.activeElement
    if (!first || !last || !panel.value.contains(active) || active === panel.value ||
      (e.shiftKey ? active === first : active === last)) {
      e.preventDefault()
      ;(e.shiftKey ? last : first)?.focus()
      if (!first) panel.value.focus()
    }
  }
}

onMounted(() => {
  syncOpen(props.modelValue)
  window.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  syncOpen(false)
  window.removeEventListener('keydown', handleKeydown)
})

watch(
  () => props.modelValue,
  syncOpen,
  { immediate: true }
)

const widthClasses: Record<string, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-xl',
  '2xl': 'max-w-2xl',
}
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="modelValue"
        class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-gray-900/50 backdrop-blur-sm overflow-y-auto"
        @click.self="closeModal"
      >
        <Transition
          enter-active-class="transition duration-200 ease-out"
          enter-from-class="opacity-0 scale-95 translate-y-2"
          enter-to-class="opacity-100 scale-100 translate-y-0"
          leave-active-class="transition duration-150 ease-in"
          leave-from-class="opacity-100 scale-100 translate-y-0"
          leave-to-class="opacity-0 scale-95 translate-y-2"
        >
          <div
            v-if="modelValue"
            ref="panel"
            role="dialog"
            aria-modal="true"
            :aria-label="title || '对话框'"
            tabindex="-1"
            :class="[
              'w-full bg-white rounded-2xl shadow-xl border border-gray-100 flex flex-col overflow-hidden my-8',
              widthClasses[maxWidth] || 'max-w-md',
            ]"
          >
            <!-- Modal Header -->
            <div class="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
              <h3 class="text-base font-semibold text-gray-900">
                {{ title }}
              </h3>
              <button
                type="button"
                aria-label="关闭对话框"
                class="text-gray-400 hover:text-gray-600 p-1 rounded-lg hover:bg-gray-100 transition-colors"
                @click="closeModal"
              >
                <X class="w-5 h-5" />
              </button>
            </div>

            <!-- Modal Body -->
            <div class="px-6 py-5 overflow-y-auto max-h-[75vh]">
              <slot />
            </div>

            <!-- Modal Footer -->
            <div
              v-if="$slots.footer"
              class="px-6 py-3.5 bg-gray-50 border-t border-gray-100 flex items-center justify-end gap-3"
            >
              <slot name="footer" />
            </div>
          </div>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>
