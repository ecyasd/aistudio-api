<script setup lang="ts">
import { ref } from 'vue'
import { HelpCircle, CheckCircle2 } from 'lucide-vue-next'
import { CONFIG_SCHEMA } from './schema.ts'

const props = defineProps<{
  schemaKey?: string
  title?: string
  content?: string
  explanation?: string
  recommended?: string | number | boolean
}>()

const show = ref(false)

const fieldDef = props.schemaKey ? CONFIG_SCHEMA[props.schemaKey] : null
const displayTitle = props.title || fieldDef?.title || ''
const displayContent = props.content || fieldDef?.description || ''
const displayExplanation = props.explanation || fieldDef?.explanation || ''
const displayRecommended = props.recommended !== undefined ? props.recommended : fieldDef?.recommendedValue
</script>

<template>
  <div
    class="relative inline-flex items-center"
    @mouseenter="show = true"
    @mouseleave="show = false"
  >
    <button
      type="button"
      class="text-muted hover:text-accent transition-colors p-0.5 rounded cursor-pointer"
      :title="displayTitle"
      @click.stop="show = !show"
    >
      <HelpCircle class="w-3.5 h-3.5" />
    </button>

    <!-- Floating Tooltip Box -->
    <Transition
      enter-active-class="transition duration-150 ease-out"
      enter-from-class="opacity-0 scale-95 translate-y-1"
      enter-to-class="opacity-100 scale-100 translate-y-0"
      leave-active-class="transition duration-100 ease-in"
      leave-from-class="opacity-100 scale-100 translate-y-0"
      leave-to-class="opacity-0 scale-95 translate-y-1"
    >
      <div
        v-if="show"
        class="absolute left-1/2 -translate-x-1/2 bottom-full mb-2 z-50 w-72 sm:w-80 bg-canvas text-fg p-3.5 rounded-xl  border border-line-strong text-xs space-y-2 pointer-events-auto"
      >
        <div class="flex items-center justify-between border-b border-line pb-1.5 font-semibold text-accent">
          <span>{{ displayTitle }}</span>
          <span
            v-if="fieldDef?.tags?.length"
            class="text-[10px] font-normal px-1.5 py-0.2 rounded bg-canvas text-muted"
          >
            {{ fieldDef.tags.join(' · ') }}
          </span>
        </div>

        <div class="text-fg leading-relaxed text-[11px]">
          {{ displayContent }}
        </div>

        <div
          v-if="displayExplanation"
          class="text-muted text-[11px] leading-relaxed pt-1 border-t border-line/80"
        >
          <strong class="text-fg">业务原理:</strong> {{ displayExplanation }}
        </div>

        <div
          v-if="displayRecommended !== undefined"
          class="flex items-center gap-1.5 text-[11px] text-success font-semibold bg-success-soft/60 px-2 py-1 rounded-lg border border-success-line/60"
        >
          <CheckCircle2 class="w-3.5 h-3.5 shrink-0 text-success" />
          <span>推荐配置: {{ String(displayRecommended) }}</span>
        </div>

        <!-- Little Arrow -->
        <div class="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-line" />
      </div>
    </Transition>
  </div>
</template>
