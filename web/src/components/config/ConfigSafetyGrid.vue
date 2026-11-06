<script setup lang="ts">
import type { SafetySettingsMap } from './types.ts'
import { Shield, ShieldAlert, ShieldCheck } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    modelValue?: SafetySettingsMap
    disabled?: boolean
  }>(),
  {
    modelValue: () => ({}),
    disabled: false,
  }
)

const emit = defineEmits<{
  'update:modelValue': [val: SafetySettingsMap]
}>()

const categories = [
  { key: 'Harassment', label: '骚扰内容', desc: '负面或恶意言语攻击' },
  { key: 'Hate', label: '仇恨言论', desc: '针对特定群体的仇恨偏见' },
  { key: 'Sexually Explicit', label: '色情内容', desc: '性暗示或露骨色情' },
  { key: 'Dangerous Content', label: '危险内容', desc: '促进危险行为或暴力' },
]

const levels = [
  { val: 1, label: '1 - 严苛', desc: '最强过滤强度', color: 'border-danger-line text-danger bg-danger-soft' },
  { val: 2, label: '2 - 较严', desc: '较高过滤强度', color: 'border-warning-line text-warning bg-warning-soft' },
  { val: 3, label: '3 - 中等', desc: '标准中等强度', color: 'border-info-line text-info bg-info-soft' },
  { val: 4, label: '4 - 宽松', desc: '宽松低强度', color: 'border-success-line text-success bg-success-soft' },
  { val: 5, label: '5 - 关闭', desc: '完全不拦截', color: 'border-line-strong text-fg bg-raised font-semibold' },
]

function getLevel(catKey: string): number {
  return props.modelValue?.[catKey] ?? 5
}

function setLevel(catKey: string, val: number) {
  if (props.disabled) return
  const next: SafetySettingsMap = { ...(props.modelValue || {}) }
  next[catKey] = val
  emit('update:modelValue', next)
}

function setAll(val: number) {
  if (props.disabled) return
  const next: SafetySettingsMap = {}
  for (const cat of categories) {
    next[cat.key] = val
  }
  emit('update:modelValue', next)
}
</script>

<template>
  <div class="space-y-3 p-4 bg-canvas/70 border border-line/80 rounded-xl">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-line/60 pb-2.5">
      <div class="flex items-center gap-2">
        <Shield class="w-4 h-4 text-accent" />
        <div>
          <h4 class="text-xs font-semibold text-fg">
            安全拦截等级
          </h4>
        </div>
      </div>

      <!-- Batch Actions -->
      <div
        v-if="!disabled"
        class="flex items-center gap-1.5"
      >
        <button
          type="button"
          class="px-2 py-1 text-[11px] font-medium bg-panel text-success border border-success-line rounded-lg hover:bg-success-soft transition-colors flex items-center gap-1 cursor-pointer"
          @click="setAll(5)"
        >
          <ShieldAlert class="w-3 h-3 text-success" />
          <span>全设为关闭拦截 (5)</span>
        </button>
        <button
          type="button"
          class="px-2 py-1 text-[11px] font-medium bg-panel text-secondary border border-line rounded-lg hover:bg-raised transition-colors flex items-center gap-1 cursor-pointer"
          @click="setAll(3)"
        >
          <ShieldCheck class="w-3 h-3 text-muted" />
          <span>全设为中等 (3)</span>
        </button>
      </div>
    </div>

    <!-- 4 Categories Grid -->
    <div class="space-y-3 pt-1">
      <div
        v-for="cat in categories"
        :key="cat.key"
        class="p-2.5 bg-panel border border-line/60 rounded-xl space-y-1.5 "
      >
        <div class="flex items-center justify-between">
          <div>
            <span class="text-xs font-semibold text-fg">{{ cat.label }}</span>
            <span class="text-[11px] text-muted ml-2">{{ cat.desc }}</span>
          </div>
          <span
            class="text-[11px] font-mono font-semibold px-2 py-0.5 rounded"
            :class="getLevel(cat.key) === 5 ? 'bg-success-soft text-success' : 'bg-warning-soft text-warning'"
          >
            当前: {{ getLevel(cat.key) === 5 ? '已关闭拦截 (5)' : `等级 ${getLevel(cat.key)}` }}
          </span>
        </div>

        <div class="grid grid-cols-5 gap-1.5 pt-1">
          <button
            v-for="lvl in levels"
            :key="lvl.val"
            type="button"
            :disabled="disabled"
            class="py-1.5 px-1 text-center rounded-lg border text-xs font-mono transition-all cursor-pointer select-none disabled:opacity-50"
            :class="[
              getLevel(cat.key) === lvl.val
                ? `${lvl.color} ring-2 ring-accent/20  font-semibold`
                : 'bg-canvas/80 text-secondary border-line/80 hover:bg-raised',
            ]"
            :title="lvl.desc"
            @click="setLevel(cat.key, lvl.val)"
          >
            <div class="font-semibold">
              {{ lvl.val }}
            </div>
            <div class="text-[9px] truncate">
              {{ lvl.val === 5 ? '关闭' : lvl.val === 1 ? '最严' : '中等' }}
            </div>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
