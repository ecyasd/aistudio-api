<script setup lang="ts">
interface Props {
  variant?: 'green' | 'red' | 'amber' | 'blue' | 'gray'
  size?: 'sm' | 'md'
}

withDefaults(defineProps<Props>(), {
  variant: 'gray',
  size: 'md',
})
</script>

<template>
  <span
    class="inline-flex items-center justify-center font-medium rounded-full transition-colors"
    :class="[
      size === 'sm' ? 'px-2 py-0.5 text-xs' : 'px-2.5 py-1 text-xs',
      {
        'bg-success-soft text-success border border-success-line/60': variant === 'green',
        'bg-danger-soft text-danger border border-danger-line/60': variant === 'red',
        'bg-warning-soft text-warning border border-warning-line/60': variant === 'amber',
        'bg-info-soft text-info border border-info-line/60': variant === 'blue',
        'bg-raised text-secondary border border-line/60': variant === 'gray',
      },
    ]"
  >
    <slot />
  </span>
</template>
