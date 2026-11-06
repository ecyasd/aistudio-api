<script setup lang="ts">
interface Props {
  modelValue: string | number | null | undefined
  label: string
  description?: string
  placeholder?: string
  type?: 'text' | 'number'
  disabled?: boolean
}

withDefaults(defineProps<Props>(), {
  description: '',
  placeholder: '',
  type: 'text',
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [val: string | number | null]
}>()

function handleInput(e: Event) {
  const target = e.target as HTMLInputElement
  const raw = target.value
  if (raw === '') {
    emit('update:modelValue', null)
  } else {
    emit('update:modelValue', raw)
  }
}
</script>

<template>
  <div class="space-y-1.5">
    <div class="flex items-center justify-between">
      <label class="block text-xs font-semibold text-secondary">
        {{ label }}
      </label>
      <span
        v-if="description"
        class="text-[11px] text-muted"
      >
        {{ description }}
      </span>
    </div>

    <input
      :type="type"
      :value="modelValue ?? ''"
      :placeholder="placeholder"
      :disabled="disabled"
      class="w-full px-3 py-2 bg-canvas border border-line rounded-xl text-xs font-mono outline-none focus:bg-panel focus:border-accent-line focus:ring-2 focus:ring-accent/20 transition-all disabled:opacity-50"
      @input="handleInput"
    >
  </div>
</template>
