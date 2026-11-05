<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { Activity, ArrowDownToLine, ArrowUpFromLine, RefreshCw } from 'lucide-vue-next'
import { fetchUsage, type UsageReport, type UsageRecord } from '@/api/usage.ts'
import { usePolling } from '@/composables/usePolling.ts'
import StatCard from '@/components/ui/StatCard.vue'

const days = ref(7)
const model = ref('')
const page = ref(1)
const report = ref<UsageReport | null>(null)
const loading = ref(false)
const error = ref('')
let controller: AbortController | null = null
let sequence = 0
let disposed = false

async function loadData() {
  if (disposed) return false
  const current = ++sequence
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const next = await fetchUsage(days.value, model.value, page.value, controller.signal)
    if (disposed || current !== sequence) return false
    report.value = next
    return true
  } catch (cause) {
    if (disposed || current !== sequence || (cause instanceof DOMException && cause.name === 'AbortError')) return false
    error.value = cause instanceof Error ? cause.message : '用量读取失败，请重试'
    return false
  } finally {
    if (!disposed && current === sequence) loading.value = false
  }
}

watch([days, model], () => { page.value = 1 }, { flush: 'sync' })
watch([days, model, page], () => { void loadData() })
usePolling(loadData, 15000)
onUnmounted(() => { disposed = true; sequence++; controller?.abort() })

const totals = computed(() => report.value?.totals)
const maxPage = computed(() => Math.max(1, Math.ceil((report.value?.total || 0) / 25)))
const number = (value: number | null | undefined) => value == null ? '未知' : value.toLocaleString('zh-CN')
const seconds = (value: number | null) => value == null ? '未知' : `${(value / 1000).toFixed(2)} s`
const labels: Record<UsageRecord['outcome'], string> = { success: '成功', recovered: '重试后成功', failed: '失败', cancelled: '已取消' }
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 class="text-xl font-bold text-gray-900">
          用量与请求
        </h1>
        <p class="mt-1 text-sm text-gray-600">
          查看酒馆请求、实际上游调用次数及已报告的 Token。
        </p>
      </div>
      <button
        type="button"
        class="inline-flex min-h-11 items-center gap-2 rounded-xl border border-gray-300 bg-white px-4 text-sm font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-brand-500 disabled:opacity-50"
        :disabled="loading"
        @click="loadData"
      >
        <RefreshCw
          class="h-4 w-4"
          aria-hidden="true"
        />{{ loading ? '读取中…' : '刷新' }}
      </button>
    </div>
    <div class="flex flex-wrap items-end gap-4 rounded-2xl border border-gray-200 bg-white p-4">
      <label class="grid gap-1 text-sm font-medium text-gray-700">
        时间范围
        <select
          v-model="days"
          class="min-h-11 rounded-lg border border-gray-300 bg-white px-3 focus-visible:outline-2 focus-visible:outline-brand-500"
        >
          <option :value="1">最近 24 小时</option><option :value="7">最近 7 天</option><option :value="30">最近 30 天</option>
        </select>
      </label>
      <label class="grid min-w-0 gap-1 text-sm font-medium text-gray-700">
        模型
        <select
          v-model="model"
          class="min-h-11 max-w-full rounded-lg border border-gray-300 bg-white px-3 focus-visible:outline-2 focus-visible:outline-brand-500"
        >
          <option value="">全部模型</option>
          <option
            v-for="name in report?.models || []"
            :key="name"
            :value="name"
          >{{ name }}</option>
        </select>
      </label>
    </div>
    <p
      v-if="error"
      role="alert"
      class="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700"
    >
      {{ error }}
    </p>
    <div
      class="grid grid-cols-1 gap-4 sm:grid-cols-3"
      :aria-busy="loading"
    >
      <StatCard
        label="酒馆请求"
        :value="number(totals?.requests)"
        :sub-value="`成功 ${number(totals?.success)}`"
      >
        <template #icon>
          <Activity
            class="h-4 w-4 text-brand-600"
            aria-hidden="true"
          />
        </template>
      </StatCard>
      <StatCard
        label="已报告输入 Token"
        :value="number(totals?.prompt_tokens)"
        :sub-value="`缓存 ${number(totals?.cached_tokens)}`"
      >
        <template #icon>
          <ArrowUpFromLine
            class="h-4 w-4 text-blue-600"
            aria-hidden="true"
          />
        </template>
      </StatCard>
      <StatCard
        label="已报告输出 Token"
        :value="number(totals?.completion_tokens)"
        :sub-value="`上游调用 ${number(totals?.upstream_calls)}`"
      >
        <template #icon>
          <ArrowDownToLine
            class="h-4 w-4 text-emerald-600"
            aria-hidden="true"
          />
        </template>
      </StatCard>
    </div>
    <div class="overflow-hidden rounded-2xl border border-gray-200 bg-white">
      <div class="border-b border-gray-200 p-4">
        <h2 class="font-semibold text-gray-900">
          已结束的请求
        </h2>
        <p class="mt-1 text-xs text-gray-600">
          每次客户端请求记录一行；用量累计已报告的上游轮次，含重试和续写。未报告的部分显示“未知”。
        </p>
      </div>
      <div
        class="overflow-x-auto"
        role="region"
        aria-label="请求记录表"
        tabindex="0"
      >
        <table class="w-full min-w-[850px] text-left text-sm">
          <thead class="bg-gray-50 text-xs font-semibold text-gray-600">
            <tr>
              <th
                scope="col"
                class="p-4"
              >
                时间 / 请求 ID
              </th><th
                scope="col"
                class="p-4"
              >
                模型 / 通道
              </th>
              <th
                scope="col"
                class="p-4"
              >
                结果
              </th><th
                scope="col"
                class="p-4"
              >
                耗时 / 首段正文
              </th>
              <th
                scope="col"
                class="p-4"
              >
                输入 / 输出 Token
              </th><th
                scope="col"
                class="p-4"
              >
                已报告轮次
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-100">
            <tr
              v-for="item in report?.items || []"
              :key="item.request_id"
              class="hover:bg-gray-50"
            >
              <td class="p-4 text-gray-700">
                <div>{{ new Date(item.time * 1000).toLocaleString('zh-CN') }}</div><div class="mt-1 font-mono text-xs text-gray-500">
                  {{ item.request_id }}
                </div>
              </td>
              <td class="max-w-64 break-words p-4 font-medium text-gray-900">
                <div>{{ item.model || '未知模型' }}</div><div class="mt-1 text-xs font-normal text-gray-500">
                  {{ item.channel === 'build' ? 'Build 助手' : 'Playground' }} · {{ item.stream ? '流式' : '非流式' }}
                </div>
              </td>
              <td
                class="p-4 font-medium"
                :class="item.outcome === 'failed' ? 'text-rose-700' : item.outcome === 'cancelled' ? 'text-gray-600' : 'text-emerald-700'"
              >
                {{ labels[item.outcome] }}
              </td>
              <td class="p-4 text-gray-700">
                <div>{{ seconds(item.duration_ms) }}</div><div class="mt-1 text-xs text-gray-500">
                  首段 {{ seconds(item.first_body_ms) }}
                </div>
              </td>
              <td class="p-4 font-mono text-gray-700">
                <div>{{ number(item.prompt_tokens) }} / {{ number(item.completion_tokens) }}</div><div class="mt-1 text-xs text-gray-500">
                  缓存 {{ number(item.cached_tokens) }}
                </div>
              </td>
              <td class="p-4 text-gray-700">
                <div>{{ item.reported_calls }} / {{ item.upstream_calls }}</div><div
                  v-if="!item.usage_complete"
                  class="mt-1 text-xs text-amber-700"
                >
                  用量不完整
                </div>
              </td>
            </tr>
            <tr v-if="!report?.items.length">
              <td
                colspan="6"
                class="p-10 text-center text-gray-500"
                role="status"
              >
                {{ loading ? '正在读取请求…' : '此范围还没有已结束的请求' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="flex items-center justify-between gap-3 border-t border-gray-200 p-4 text-sm text-gray-600">
        <span>第 {{ page }} / {{ maxPage }} 页 · {{ report?.total || 0 }} 条</span>
        <div class="flex gap-2">
          <button
            type="button"
            class="min-h-11 rounded-lg border border-gray-300 px-3 disabled:opacity-40"
            :disabled="loading || page <= 1"
            @click="page--"
          >
            上一页
          </button>
          <button
            type="button"
            class="min-h-11 rounded-lg border border-gray-300 px-3 disabled:opacity-40"
            :disabled="loading || page >= maxPage"
            @click="page++"
          >
            下一页
          </button>
        </div>
      </div>
    </div>
    <p class="text-xs leading-relaxed text-gray-600">
      保留最近 {{ report?.retention.days || 30 }} 天、最多 {{ number(report?.retention.max_records || 1000) }} 条请求的数字摘要。汇总仅覆盖保留记录，不包含升级前的历史请求。未保存提示词、正文、思考、签名或密钥。
    </p>
  </div>
</template>
