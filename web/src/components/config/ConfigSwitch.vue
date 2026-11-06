<script setup lang="ts">
interface Props {
  modelValue: boolean
  label: string
  description?: string
  disabled?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  description: '',
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

function toggle() {
  if (!props.disabled) {
    emit('update:modelValue', !props.modelValue)
  }
}
</script>

<template>
  <div
    class="flex items-center justify-between p-3.5 bg-canvas/70 border border-line/70 rounded-xl transition-colors hover:bg-canvas select-none cursor-pointer"
    :class="{ 'opacity-60 cursor-not-allowed': disabled }"
    @click="toggle"
  >
    <div class="pr-4">
      <div class="text-xs font-semibold text-fg">
        {{ label }}
      </div>
      <div
        v-if="description"
        class="text-[11px] text-muted mt-0.5"
      >
        {{ description }}
      </div>
    </div>

    <button
      type="button"
      role="switch"
      :aria-label="label"
      :aria-checked="modelValue"
      :disabled="disabled"
      class="relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none"
      :class="modelValue ? 'bg-accent' : 'bg-line-strong'"
      @click.stop="toggle"
    >
      <span
        class="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-fg ring-0 transition duration-200 ease-in-out"
        :class="modelValue ? 'translate-x-4' : 'translate-x-0'"
      />
    </button>
  </div>
</template>
