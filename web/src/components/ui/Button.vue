<script setup lang="ts">
import { Loader2 } from 'lucide-vue-next'

interface Props {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost' | 'outline'
  size?: 'sm' | 'md' | 'lg'
  loading?: boolean
  disabled?: boolean
  type?: 'button' | 'submit' | 'reset'
}

withDefaults(defineProps<Props>(), {
  variant: 'secondary',
  size: 'md',
  loading: false,
  disabled: false,
  type: 'button',
})
</script>

<template>
  <button
    :type="type"
    :disabled="disabled || loading"
    class="inline-flex items-center justify-center font-medium rounded-xl transition-all select-none disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer active:scale-[0.98]"
    :class="[
      size === 'sm' ? 'px-3 py-1.5 text-xs gap-1.5' : size === 'lg' ? 'px-5 py-2.5 text-base gap-2.5' : 'px-4 py-2 text-sm gap-2',
      {
        'bg-accent hover:brightness-110 text-on-accent focus:ring-2 focus:ring-accent/40': variant === 'primary',
        'bg-panel hover:bg-canvas text-secondary border border-line  focus:ring-2 focus:ring-line': variant === 'secondary',
        'bg-danger-soft hover:bg-danger-soft text-danger border border-danger-line focus:ring-2 focus:ring-danger': variant === 'danger',
        'hover:bg-raised text-secondary focus:ring-2 focus:ring-line': variant === 'ghost',
        'border border-line-strong text-secondary hover:bg-canvas': variant === 'outline',
      },
    ]"
  >
    <Loader2
      v-if="loading"
      class="w-4 h-4 animate-spin shrink-0"
    />
    <slot />
  </button>
</template>
