<script setup lang="ts">
interface Props {
  label: string
  value: string | number
  subValue?: string
  color?: 'default' | 'green' | 'amber' | 'red' | 'blue'
}

withDefaults(defineProps<Props>(), {
  subValue: '',
  color: 'default',
})
</script>

<template>
  <div class="bg-panel border border-line/80 rounded-2xl p-5  transition-all  hover:border-line-strong">
    <div class="flex items-center justify-between text-xs font-semibold text-muted uppercase tracking-wider mb-2">
      <span>{{ label }}</span>
      <slot name="icon" />
    </div>
    <div class="flex items-baseline gap-2">
      <div
        class="text-2xl font-semibold tracking-tight truncate max-w-full"
        :title="String(value)"
        :class="{
          'text-fg': color === 'default',
          'text-success': color === 'green',
          'text-warning': color === 'amber',
          'text-danger': color === 'red',
          'text-info': color === 'blue',
        }"
      >
        {{ value }}
      </div>
      <span
        v-if="subValue"
        class="text-xs text-muted font-medium"
      >{{ subValue }}</span>
    </div>
  </div>
</template>
