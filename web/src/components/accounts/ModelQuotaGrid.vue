<script setup lang="ts">
import type { AccountWithStats } from '@/types/accounts.ts'
import type { ModelDailyDetail } from '@/types/rotation.ts'
import { formatDate } from '@/utils/format.ts'
import { Flame, RotateCcw, Brain, Database } from 'lucide-vue-next'

const props = defineProps<{
  account: AccountWithStats
}>()

const emit = defineEmits<{
  clearModel: [model: string]
}>()

function getModelDetail(modelKey: string): ModelDailyDetail | null {
  const today = props.account.today_models || {}
  return today[modelKey] || null
}
</script>

<template>
  <div class="mt-3 pt-3 border-t border-line space-y-2 bg-canvas/80 p-3.5 rounded-xl">
    <div class="flex items-center justify-between text-[11px] font-semibold text-secondary">
      <span class="flex items-center gap-1.5">
        <Flame class="w-3.5 h-3.5 text-warning" />
        <span>各模型今日调用与配额</span>
      </span>
      <span class="text-muted font-normal font-mono">
        最后调用: {{ formatDate(account.last_used) }}
      </span>
    </div>

    <div
      v-if="account.model_requests && Object.keys(account.model_requests).length"
      class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5 pt-1"
    >
      <div
        v-for="(reqCount, modelKey) in account.model_requests"
        :key="String(modelKey)"
        class="p-3 bg-panel rounded-xl border border-line/90 flex flex-col justify-between text-xs  gap-2"
      >
        <div>
          <div class="flex items-center justify-between gap-1 font-mono">
            <span
              class="font-semibold text-fg truncate text-[11px]"
              :title="String(modelKey)"
            >
              {{ String(modelKey).replace(/^models\//, '') }}
            </span>
            <div class="shrink-0 flex items-center gap-1">
              <button
                v-if="account.model_cooldowns && account.model_cooldowns[String(modelKey)]"
                type="button"
                class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-danger-soft text-danger hover:bg-danger-soft border border-danger-line cursor-pointer flex items-center gap-0.5"
                title="点击重置该模型锁定"
                @click="emit('clearModel', String(modelKey))"
              >
                <RotateCcw class="w-2.5 h-2.5" />
                <span>429 ({{ account.model_cooldowns[String(modelKey)] }}s)</span>
              </button>
              <span
                v-else-if="account.model_drip_mode && account.model_drip_mode[String(modelKey)]"
                class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-info-soft text-info border border-info-line"
              >
                滴灌
              </span>
              <span
                v-else-if="account.model_rate_limited && account.model_rate_limited[String(modelKey)]"
                class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-warning-soft text-warning border border-warning-line"
              >
                429: {{ account.model_rate_limited[String(modelKey)] }}
              </span>
              <span
                v-else
                class="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-success-soft text-success border border-success-line"
              >
                就绪
              </span>
            </div>
          </div>

          <!-- Usage & Quota Progress Bar -->
          <div
            v-if="getModelDetail(String(modelKey))"
            class="mt-2 space-y-1 font-mono text-[10px]"
          >
            <div class="flex justify-between text-muted">
              <span>今日调用: <strong>{{ reqCount }}</strong> / {{ getModelDetail(String(modelKey))!.configured_quota }}</span>
              <span>{{ Math.round(getModelDetail(String(modelKey))!.quota_used_percent) }}%</span>
            </div>
            <div class="w-full bg-raised rounded-full h-1.5 overflow-hidden">
              <div
                class="h-1.5 rounded-full transition-all duration-300"
                :class="[
                  getModelDetail(String(modelKey))!.quota_used_percent >= 100
                    ? 'bg-warning'
                    : getModelDetail(String(modelKey))!.quota_used_percent >= 80
                      ? 'bg-warning'
                      : 'bg-accent',
                ]"
                :style="{ width: `${Math.min(100, Math.round(getModelDetail(String(modelKey))!.quota_used_percent))}%` }"
              />
            </div>
          </div>
          <div
            v-else
            class="mt-1.5 text-[10px] text-muted font-mono"
          >
            今日调用: <strong>{{ reqCount }}</strong> 次
          </div>
        </div>

        <!-- Token breakdown pills if available -->
        <div
          v-if="getModelDetail(String(modelKey)) && (getModelDetail(String(modelKey))!.reasoning_tokens > 0 || getModelDetail(String(modelKey))!.cached_tokens > 0)"
          class="pt-1.5 border-t border-line flex items-center gap-1.5 flex-wrap font-mono text-[10px]"
        >
          <span
            v-if="getModelDetail(String(modelKey))!.reasoning_tokens > 0"
            class="inline-flex items-center gap-0.5 text-violet bg-violet-soft px-1.5 py-0.2 rounded border border-violet-line/50"
            title="思考推理 Token"
          >
            <Brain class="w-2.5 h-2.5" />
            <span>{{ getModelDetail(String(modelKey))!.reasoning_tokens.toLocaleString() }}</span>
          </span>
          <span
            v-if="getModelDetail(String(modelKey))!.cached_tokens > 0"
            class="inline-flex items-center gap-0.5 text-success bg-success-soft px-1.5 py-0.2 rounded border border-success-line/50"
            title="命中缓存 Token"
          >
            <Database class="w-2.5 h-2.5" />
            <span>{{ getModelDetail(String(modelKey))!.cached_tokens.toLocaleString() }}</span>
          </span>
        </div>
      </div>
    </div>
    <div
      v-else
      class="text-[11px] text-muted italic py-1"
    >
      今日暂无模型调用记录
    </div>
  </div>
</template>
