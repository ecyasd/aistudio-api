<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { diagnosticContext } from '@/utils/diagnosticContext.ts'
import { ChevronLeft, ChevronRight, FileWarning, RefreshCw, Search } from 'lucide-vue-next'
import { useErrorsStore } from '@/stores/errors.ts'
import { usePolling } from '@/composables/usePolling.ts'
import { formatDate } from '@/utils/format.ts'
import type { ErrorRecord, ResponseDiagnostic } from '@/types/errors.ts'
import Button from '@/components/ui/Button.vue'
import Modal from '@/components/ui/Modal.vue'

const errorsStore = useErrorsStore()
const { result, items, diagnosticItems, statusFilter, categoryFilter, accountFilter, requestFilter, loading, error, lastUpdated, page, matchesFilters } = storeToRefs(errorsStore)
const route = useRoute()
const router = useRouter()
watch(() => route.query, query => {
  const context = diagnosticContext(query)
  accountFilter.value = context.account
  requestFilter.value = context.request
  statusFilter.value = ''
  categoryFilter.value = ''
}, { immediate: true })
function clearFilters() {
  statusFilter.value = ''
  categoryFilter.value = ''
  void router.replace({ path: '/error-records', query: {} })
}
const selected = ref<ErrorRecord | null>(null)
const detailsOpen = computed({
  get: () => selected.value !== null,
  set: (open: boolean) => { if (!open) selected.value = null },
})

const categories = [
  { value: 'session_expired', label: '登录态失效' },
  { value: 'authentication', label: '身份验证' },
  { value: 'rate_limit', label: '配额与限流' },
  { value: 'timeout', label: '超时' },
  { value: 'browser', label: '浏览器' },
  { value: 'network', label: '网络' },
  { value: 'validation', label: '请求校验' },
  { value: 'upstream', label: '上游服务' },
  { value: 'internal', label: '内部异常' },
  { value: 'cancelled', label: '已取消' },
]
const statuses = [400, 401, 403, 404, 408, 422, 429, 499, 500, 502, 503, 504]
const outcomeLabels: Record<ErrorRecord['outcome'], string> = {
  retrying: '自动重试中',
  failed: '最终失败',
  recovered: '重试后成功',
  cancelled: '已取消',
  warning: '警告',
}
const diagnosticOutcomeLabels: Record<ResponseDiagnostic['outcome'], string> = {
  success: '成功',
  recovered: '重试后成功',
  failed: '失败',
  cancelled: '已取消',
}
const phaseLabels: Record<ResponseDiagnostic['phase_sequence'][number], string> = {
  body: '正文',
  thought: '思考',
  empty_thought: '空思考 / 签名',
  finish: '结束',
}
const finishReasonLabels: Record<number, string> = {
  0: '未指定 (0)',
  1: '正常结束 (STOP)',
  2: '达到输出上限 (MAX_TOKENS)',
  3: '安全限制 (SAFETY)',
  4: '引用限制 (RECITATION)',
  5: '语言限制 (LANGUAGE)',
  6: '其他原因 (OTHER)',
}

function diagnosticOutcomeClass(outcome: ResponseDiagnostic['outcome']) {
  return outcomeClass(outcome === 'success' ? 'recovered' : outcome)
}

function diagnosticSequence(record: ResponseDiagnostic) {
  return record.phase_sequence.map(phase => phaseLabels[phase]).join(' → ') || '未收到内容片段'
}

function diagnosticFinish(record: ResponseDiagnostic) {
  return record.finish_reasons.map(reason => finishReasonLabels[reason] ?? `代码 ${reason}`).join(' → ') || '未收到结束原因'
}

function diagnosticSignals(record: ResponseDiagnostic) {
  const signals: string[] = []
  if (record.thought_after_body) signals.push(`正文后思考片段 ${record.thought_after_body} 个`)
  if (record.marker_count) signals.push(`正文含 [:数字] 标记 ${record.marker_count} 处`)
  if (record.empty_thought_parts) signals.push(`空思考 / 签名片段 ${record.empty_thought_parts} 个`)
  if (record.flags.includes('content_after_finish')) signals.push('结束信号后仍有内容')
  if (record.flags.includes('sequence_limited')) signals.push('顺序记录已达上限')
  if (record.flags.includes('continuation_skipped_budget')) signals.push('抗截断未追加：历史预算不足')
  if (record.flags.includes('continuation_skipped_unsupported')) signals.push('抗截断未追加：包含暂不支持回放的内容')
  return signals
}

function elapsed(value: number | undefined) {
  return value == null ? '未记录' : `${(value / 1000).toFixed(3)} 秒`
}

function stage(record: ResponseDiagnostic, start: string, end: string) {
  const from = record.timings_ms?.[start]
  const to = record.timings_ms?.[end]
  return from == null || to == null || to < from ? '未记录' : elapsed(to - from)
}

const preparationMilestones = [
  ['browser_launch_started', '开始启动浏览器'], ['browser_ready', '浏览器已就绪'],
  ['browser_context_ready', '账号页面已创建'], ['browser_cookies_loaded', '账号凭据已载入'],
  ['browser_navigation_ready', '页面与身份已就绪'], ['botguard_hooks_ready', '捕获钩子已就绪'],
  ['botguard_editor_ready', '编辑器已就绪'], ['temporary_chat_ready', '临时聊天检查完成'],
  ['botguard_prompt_filled', '准备输入已填入'], ['botguard_run_dispatched', '模板捕获已触发'],
  ['botguard_template_ready', '模板已捕获'], ['session_checkpoint_done', '会话检查点完成'],
] as const

const preparationReasons: Record<string, string> = {
  warm_reuse: '复用热会话', service_start: '服务启动后的首次准备', idle_recycle: '空闲回收后重新准备',
  account_change: '切换账号', credentials_change: '账号凭据已更新', forced_refresh: '主动刷新会话',
  browser_recovery: '浏览器重新准备', page_recovery: '账号页面重新准备', session_not_ready: '会话尚未就绪',
}
const transportPhases: Record<string, string> = {
  fetch_pending: '等待上游响应头', read_pending: '等待上游下一片段', awaiting_ack: '等待片段确认',
  binding_missing: '推送通道缺失', page_unresponsive: '浏览器页面无响应', state_missing: '原流状态丢失',
}

function recordedPreparation(record: ResponseDiagnostic) {
  return preparationMilestones.filter(([key]) => record.timings_ms?.[key] != null)
}

function categoryLabel(category: string) {
  return categories.find(item => item.value === category)?.label ?? category
}

function outcomeClass(outcome: ErrorRecord['outcome']) {
  if (outcome === 'recovered') return 'bg-emerald-50 text-emerald-700 border-emerald-100'
  if (outcome === 'retrying' || outcome === 'warning') return 'bg-amber-50 text-amber-800 border-amber-100'
  if (outcome === 'cancelled') return 'bg-gray-100 text-gray-600 border-gray-200'
  return 'bg-rose-50 text-rose-700 border-rose-100'
}

async function poll() {
  if (!await errorsStore.reload()) throw new Error('错误记录轮询失败')
}

usePolling(poll, 5000)
watch([statusFilter, categoryFilter, accountFilter, requestFilter], () => {
  selected.value = null
  void errorsStore.refresh()
})
watch(items, (records) => {
  if (selected.value) selected.value = records.find(record => record.id === selected.value?.id) ?? null
})
onUnmounted(errorsStore.cancel)
</script>

<template>
  <div class="space-y-5">
    <div class="flex flex-col sm:flex-row sm:items-start justify-between gap-4">
      <div>
        <h2 class="text-xl font-bold text-gray-900 tracking-tight">
          实时错误
        </h2>
        <p class="mt-2 text-sm text-gray-500 leading-relaxed">
          API 失败、流式中断及自动重试会显示在这里。页面可见时每 5 秒刷新，时间以本机时区显示。
        </p>
      </div>
      <Button
        variant="outline"
        :loading="loading"
        class="shrink-0 self-start"
        @click="errorsStore.refresh()"
      >
        <RefreshCw class="w-4 h-4" />
        刷新最新记录
      </Button>
    </div>

    <div class="rounded-xl bg-blue-50 border border-blue-100 px-4 py-3 text-xs text-blue-800 leading-relaxed">
      仅显示最近 {{ result?.retention.minutes ?? 10 }} 分钟实时错误，服务重启后清空。
      <template v-if="result">
        最多 {{ result.retention.max_records }} 条。
      </template>
      不包含请求正文、模型回复或凭据。
      <span>重试成功的记录会标为“重试后成功”；流式请求即使 HTTP 返回 200，也可能有后续失败记录。</span>
    </div>

    <div
      v-if="accountFilter || requestFilter"
      class="rounded-xl border border-brand-100 bg-brand-50 px-4 py-3 text-xs text-gray-700 space-y-2"
    >
      <p
        v-if="accountFilter"
        class="break-all"
      >
        账号范围：<span class="font-mono">{{ accountFilter }}</span>
      </p>
      <p
        v-if="requestFilter"
        class="break-all"
      >
        请求链路：<span class="font-mono">{{ requestFilter }}</span>（包含跨账号重试）
      </p>
      <p>账号与请求范围同时用于响应探测和错误记录；仅保留最近 10 分钟。</p>
      <RouterLink
        to="/error-records"
        class="inline-block py-1 text-brand-700 underline underline-offset-4"
      >
        查看全部最近请求
      </RouterLink>
    </div>

    <section
      aria-labelledby="response-diagnostics-title"
      class="bg-white border border-gray-200 rounded-2xl overflow-hidden"
    >
      <div class="px-4 sm:px-5 py-4 border-b border-gray-100">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h3
            id="response-diagnostics-title"
            class="text-sm font-semibold text-gray-900"
          >
            响应探测
          </h3>
          <span class="text-xs text-gray-500">最近 {{ diagnosticItems.length }} 次请求 · 按账号 / 请求范围，独立于错误码及页码</span>
        </div>
        <p class="mt-2 text-xs text-gray-500 leading-relaxed">
          成功与失败请求均记录，只保留顺序和计数，不含思考、正文或签名原文。
          最近 {{ result?.diagnostics?.retention.minutes ?? 10 }} 分钟，最多 {{ result?.diagnostics?.retention.max_records ?? 100 }} 条，过期或服务重启后清空。
          本页显示最新 25 条。
          上游次数按开始重放的尝试计数。
          观察信号用于定位响应过程，不直接判定为故障。
        </p>
        <p
          v-if="error && diagnosticItems.length"
          class="mt-2 text-xs text-amber-800"
        >
          显示上次读取且未过期的响应探测，最新状态尚未确认。
        </p>
      </div>
      <div
        v-if="!result"
        class="px-5 py-8 text-center text-sm text-gray-500"
      >
        {{ error ? '响应探测读取未成功，请重试。' : '正在读取最近响应…' }}
      </div>
      <div
        v-else-if="!diagnosticItems.length"
        class="px-5 py-8 text-center text-sm text-gray-500"
      >
        {{ error ? '没有尚未过期的响应探测缓存，请重试读取。' : '暂无响应探测记录，下一次生成结束后会自动显示。' }}
      </div>
      <div
        v-else
        class="divide-y divide-gray-100"
      >
        <details
          v-for="record in diagnosticItems"
          :key="record.request_id"
          class="group px-4 sm:px-5 py-3 open:bg-gray-50/50"
        >
          <summary class="cursor-pointer list-none rounded-lg focus-visible:outline-2 focus-visible:outline-brand-500">
            <div class="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs">
              <span class="text-gray-500">{{ formatDate(record.time, { withSeconds: true }) }}</span>
              <span
                :class="diagnosticOutcomeClass(record.outcome)"
                class="rounded-lg border px-2 py-0.5 font-medium"
              >{{ diagnosticOutcomeLabels[record.outcome] }}</span>
              <span class="rounded-md bg-gray-100 px-2 py-0.5 text-gray-600">{{ record.auto_continue ? '抗截断' : '普通模式' }}</span>
              <span class="text-gray-500">{{ record.stream ? '流式' : '非流式' }}</span>
              <span class="text-gray-700 break-all">{{ record.model || '未记录模型' }}</span>
              <span class="ml-auto text-gray-500">上游 {{ record.upstream_calls }} 次 · 重试 {{ record.retries }} 次</span>
              <ChevronRight class="w-3.5 h-3.5 text-gray-400 transition-transform group-open:rotate-90" />
            </div>
            <p class="mt-2 text-xs text-gray-700 break-words leading-relaxed">
              顺序：{{ diagnosticSequence(record) }}
            </p>
            <div
              v-if="diagnosticSignals(record).length"
              class="mt-2 flex flex-wrap gap-1.5"
            >
              <span
                v-for="signal in diagnosticSignals(record)"
                :key="signal"
                class="rounded-md border border-amber-100 bg-amber-50 px-2 py-0.5 text-[11px] text-amber-800"
              >{{ signal }}</span>
            </div>
            <p class="mt-2 text-xs text-gray-500 break-words">
              结束：{{ diagnosticFinish(record) }}
            </p>
          </summary>
          <dl class="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-3 border-t border-gray-100 pt-3 text-xs">
            <div class="sm:col-span-2">
              <dt class="text-gray-400">
                关联请求 ID
              </dt>
              <dd class="mt-1 font-mono text-gray-700 break-all">
                <RouterLink
                  :to="{ path: '/error-records', query: { request_id: record.request_id } }"
                  class="text-brand-700 underline underline-offset-4"
                >
                  {{ record.request_id }}
                </RouterLink>
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">
                实际使用账号
              </dt>
              <dd class="mt-1 flex flex-wrap gap-2 font-mono">
                <RouterLink
                  v-for="id in record.account_ids ?? []"
                  :key="id"
                  :to="{ path: '/accounts', query: { account_id: id } }"
                  class="text-brand-700 underline underline-offset-4 break-all"
                >
                  {{ id }}
                </RouterLink>
                <span
                  v-if="!record.account_ids?.length"
                  class="text-gray-500"
                >未租用账号 / 旧记录</span>
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">
                传输等待 / 原流恢复
              </dt>
              <dd class="mt-1 text-gray-700">
                {{ transportPhases[record.transport_wait_phase ?? ''] ?? '未记录等待阶段' }} · 探测 {{ record.transport_probes ?? '未记录' }} 次 · 恢复尝试 {{ record.transport_repair_attempts ?? '未记录' }} 次
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">
                内容计数
              </dt>
              <dd class="mt-1 text-gray-700">
                正文 {{ record.body_chars }} 字符 · 思考 {{ record.thought_chars }} 字符
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">
                耗时 / HTTP 传输状态
              </dt>
              <dd class="mt-1 text-gray-700">
                {{ (record.duration_ms / 1000).toFixed(1) }} 秒 · {{ record.transport_status == null ? '未发送响应' : `HTTP ${record.transport_status}` }}
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">
                输入规模
              </dt>
              <dd class="mt-1 text-gray-700">
                输入 {{ record.prompt_tokens == null ? '未知' : record.prompt_tokens.toLocaleString('en-US') }} token ·
                请求体 {{ record.request_body_bytes == null ? '未记录' : `${(record.request_body_bytes / 1024).toFixed(1)} KiB` }}
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">
                准备 / 发送至上游首包
              </dt>
              <dd class="mt-1 text-gray-700">
                {{ stage(record, 'account_preparation_started', 'account_ready') }} /
                {{ stage(record, 'transport_dispatched', 'upstream_first_byte') }}
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">
                会话准备原因 / 空闲回收阈值
              </dt>
              <dd class="mt-1 text-gray-700">
                {{ preparationReasons[record.session_preparation_reason ?? ''] ?? '未记录' }} /
                {{ record.browser_idle_timeout_s == null ? '未记录' : record.browser_idle_timeout_s === 0 ? '不自动回收' : `${record.browser_idle_timeout_s} 秒` }}
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">
                首段思考 / 首段正文（自请求进入）
              </dt>
              <dd class="mt-1 text-gray-700">
                {{ elapsed(record.timings_ms?.first_thought) }} / {{ elapsed(record.timings_ms?.first_body) }}
              </dd>
            </div>
            <div
              v-if="recordedPreparation(record).length"
              class="sm:col-span-2"
            >
              <dt class="text-gray-400">
                准备里程碑（自请求进入）
              </dt>
              <dd class="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-gray-700">
                <span
                  v-for="[key, label] in recordedPreparation(record)"
                  :key="key"
                >
                  {{ label }}：{{ elapsed(record.timings_ms?.[key]) }}
                </span>
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">
                顺序观察计数
              </dt>
              <dd class="mt-1 text-gray-700">
                正文后思考片段 {{ record.thought_after_body }} 个 · 空思考 / 签名 {{ record.empty_thought_parts }} · 标记 {{ record.marker_count }}
              </dd>
            </div>
            <div>
              <dt class="text-gray-400">
                错误分类
              </dt>
              <dd class="mt-1 text-gray-700 break-all">
                {{ record.error_kind ? categoryLabel(record.error_kind) : '无' }}
              </dd>
            </div>
          </dl>
        </details>
      </div>
    </section>

    <div class="bg-white border border-gray-200 rounded-2xl p-4 flex flex-col sm:flex-row sm:items-end gap-3">
      <label class="flex-1 text-xs font-medium text-gray-600">
        错误码
        <select
          v-model="statusFilter"
          class="block w-full mt-1.5 bg-white border border-gray-300 rounded-xl px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-brand-200"
        >
          <option value="">全部错误码（含警告）</option>
          <option
            v-for="status in statuses"
            :key="status"
            :value="String(status)"
          >{{ status }}</option>
        </select>
      </label>
      <label class="flex-1 text-xs font-medium text-gray-600">
        分类
        <select
          v-model="categoryFilter"
          class="block w-full mt-1.5 bg-white border border-gray-300 rounded-xl px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-brand-200"
        >
          <option value="">全部分类</option>
          <option
            v-for="category in categories"
            :key="category.value"
            :value="category.value"
          >{{ category.label }}</option>
        </select>
      </label>
      <Button
        variant="ghost"
        :disabled="!statusFilter && !categoryFilter && !accountFilter && !requestFilter"
        @click="clearFilters"
      >
        清除筛选
      </Button>
    </div>

    <div
      v-if="error"
      role="alert"
      class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800"
    >
      <div class="flex flex-wrap items-center justify-between gap-3">
        <p>{{ error }}</p>
        <Button
          variant="danger"
          size="sm"
          :loading="loading"
          @click="errorsStore.reload()"
        >
          重试读取
        </Button>
      </div>
      <p
        v-if="result"
        class="mt-2 text-xs"
      >
        下方仅保留上次读取且未过期的记录，可能不匹配当前筛选或最新状态。
      </p>
    </div>

    <div class="bg-white border border-gray-200 rounded-2xl overflow-hidden">
      <div class="px-4 sm:px-5 py-3 border-b border-gray-100 flex flex-wrap items-center justify-between gap-2 text-xs text-gray-500">
        <span v-if="result">{{ error || !matchesFilters ? '当前页缓存' : '匹配记录' }}：{{ error || !matchesFilters ? items.length : result.total }} 条</span>
        <span v-else>读取服务端诊断记录</span>
        <span v-if="lastUpdated">最近读取：{{ formatDate(lastUpdated, { withSeconds: true }) }}{{ loading ? ' · 更新中…' : '' }}</span>
      </div>

      <div
        v-if="loading && !result"
        role="status"
        class="py-16 text-sm text-center text-gray-500"
      >
        正在读取错误记录…
      </div>
      <div
        v-else-if="!result && error"
        class="py-16 px-4 text-sm text-center text-gray-500"
      >
        读取未成功，请重试。
      </div>
      <div
        v-else-if="result && items.length === 0 && !error && matchesFilters"
        class="py-14 px-5 text-center"
      >
        <FileWarning class="w-8 h-8 mx-auto text-gray-300" />
        <p class="mt-3 text-sm font-medium text-gray-700">
          {{ statusFilter || categoryFilter || accountFilter || requestFilter ? '最近 10 分钟没有符合筛选条件的错误' : '暂无实时错误' }}
        </p>
        <p class="mt-2 text-xs text-gray-500">
          {{ statusFilter || categoryFilter ? '可以清除筛选，查看其他分类。' : '发生新错误后会自动显示，过期记录自动清除。' }}
        </p>
      </div>
      <div
        v-else-if="items.length"
        class="overflow-x-auto"
      >
        <table class="w-full text-sm text-left">
          <thead class="text-xs text-gray-500 bg-gray-50/70">
            <tr>
              <th class="font-medium px-4 py-3 whitespace-nowrap">
                时间 / 结果
              </th>
              <th class="font-medium px-4 py-3 whitespace-nowrap">
                错误码 / 分类
              </th>
              <th class="font-medium px-4 py-3 min-w-64">
                诊断摘要
              </th>
              <th class="font-medium px-4 py-3 whitespace-nowrap">
                操作
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-100">
            <tr
              v-for="record in items"
              :key="record.id"
              class="hover:bg-gray-50/60 align-top"
            >
              <td class="px-4 py-4 whitespace-nowrap">
                <p class="text-xs text-gray-600">
                  {{ formatDate(record.time, { withSeconds: true }) }}
                </p>
                <span
                  :class="outcomeClass(record.outcome)"
                  class="inline-block mt-2 rounded-lg border px-2 py-0.5 text-[11px] font-medium"
                >{{ outcomeLabels[record.outcome] }}</span>
              </td>
              <td class="px-4 py-4 whitespace-nowrap">
                <p class="font-mono font-semibold text-gray-800">
                  {{ record.status }}
                </p>
                <p class="mt-1 text-xs text-gray-500">
                  {{ categoryLabel(record.category) }}
                </p>
                <p
                  v-if="record.code && record.code !== record.category"
                  class="mt-1 text-[11px] text-gray-500 font-mono break-all whitespace-normal"
                >
                  {{ record.code }}
                </p>
                <p
                  v-if="record.transport_status != null && record.transport_status !== record.status"
                  class="mt-1 text-[11px] text-gray-400"
                >
                  HTTP {{ record.transport_status }}
                </p>
              </td>
              <td class="px-4 py-4">
                <p class="text-sm text-gray-800 break-words">
                  {{ record.summary }}
                </p>
                <p class="mt-1 text-[11px] text-gray-400 font-mono break-all">
                  <RouterLink
                    v-if="record.request_id"
                    :to="{ path: '/error-records', query: { request_id: record.request_id } }"
                    class="text-brand-700 underline underline-offset-4"
                  >
                    查看完整请求链路
                  </RouterLink>
                  <span v-else>无关联请求</span>
                </p>
                <p
                  v-if="record.account_id"
                  class="mt-2 text-xs font-mono break-all"
                >
                  <RouterLink
                    :to="{ path: '/accounts', query: { account_id: record.account_id } }"
                    class="text-brand-700 underline underline-offset-4"
                  >
                    账号 {{ record.account_id }}
                  </RouterLink>
                </p>
              </td>
              <td class="px-4 py-4">
                <Button
                  size="sm"
                  variant="ghost"
                  @click="selected = record"
                >
                  <Search class="w-3.5 h-3.5" />
                  详情
                </Button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div
        v-if="result"
        class="px-4 py-3 border-t border-gray-100 flex flex-wrap items-center justify-between gap-3"
      >
        <p class="text-xs text-gray-500">
          第 {{ page }} 页 · 每页最多 25 条
        </p>
        <div class="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            :disabled="loading || page === 1 || !matchesFilters"
            @click="errorsStore.previousPage()"
          >
            <ChevronLeft class="w-3.5 h-3.5" />
            上一页
          </Button>
          <Button
            size="sm"
            variant="outline"
            :disabled="loading || !result.has_more || !result.next_cursor || !matchesFilters"
            @click="errorsStore.nextPage()"
          >
            下一页
            <ChevronRight class="w-3.5 h-3.5" />
          </Button>
        </div>
      </div>
    </div>

    <Modal
      v-model="detailsOpen"
      title="错误详情"
      max-width="2xl"
    >
      <div
        v-if="selected"
        class="space-y-5"
      >
        <div>
          <span
            :class="outcomeClass(selected.outcome)"
            class="inline-block rounded-lg border px-2 py-1 text-xs font-medium"
          >{{ outcomeLabels[selected.outcome] }}</span>
          <p class="mt-3 text-sm text-gray-900 break-words leading-relaxed">
            {{ selected.summary }}
          </p>
        </div>
        <dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-3 text-xs">
          <div>
            <dt class="text-gray-400">
              时间
            </dt><dd class="mt-1 text-gray-800">
              {{ new Date(selected.time).toLocaleString('zh-CN') }}
            </dd>
          </div>
          <div>
            <dt class="text-gray-400">
              分类
            </dt><dd class="mt-1 text-gray-800">
              {{ categoryLabel(selected.category) }} · {{ selected.category }}
            </dd>
          </div>
          <div>
            <dt class="text-gray-400">
              错误码
            </dt><dd class="mt-1 font-mono text-gray-800">
              {{ selected.status }}
            </dd>
          </div>
          <div>
            <dt class="text-gray-400">
              诊断代码
            </dt><dd class="mt-1 font-mono text-gray-800 break-all">
              {{ selected.code || selected.category }}
            </dd>
          </div>
          <div>
            <dt class="text-gray-400">
              HTTP 传输状态
            </dt><dd class="mt-1 text-gray-800">
              {{ selected.transport_status ?? '未发送响应' }}{{ selected.transport_status === 200 && selected.status >= 400 ? '（响应开始后的失败）' : '' }}
            </dd>
          </div>
          <div>
            <dt class="text-gray-400">
              来源 / 操作
            </dt><dd class="mt-1 text-gray-800 break-all">
              {{ selected.source }} / {{ selected.operation || '—' }}
            </dd>
          </div>
          <div>
            <dt class="text-gray-400">
              异常类型
            </dt><dd class="mt-1 font-mono text-gray-800 break-all">
              {{ selected.exception_type || '—' }}
            </dd>
          </div>
          <div v-if="selected.model">
            <dt class="text-gray-400">
              模型
            </dt><dd class="mt-1 text-gray-800 break-all">
              {{ selected.model }}
            </dd>
          </div>
          <div v-if="selected.account_id">
            <dt class="text-gray-400">
              账号 ID
            </dt><dd class="mt-1 font-mono text-gray-800 break-all">
              {{ selected.account_id }}
            </dd>
          </div>
          <div v-if="selected.attempt != null">
            <dt class="text-gray-400">
              尝试序号
            </dt><dd class="mt-1 text-gray-800">
              {{ selected.attempt }}
            </dd>
          </div>
          <div class="sm:col-span-2">
            <dt class="text-gray-400">
              关联请求 ID
            </dt><dd class="mt-1 font-mono text-gray-800 break-all select-all">
              {{ selected.request_id || '—' }}
            </dd>
          </div>
          <div class="sm:col-span-2">
            <dt class="text-gray-400">
              记录 ID
            </dt><dd class="mt-1 font-mono text-gray-500 break-all select-all">
              {{ selected.id }}
            </dd>
          </div>
        </dl>
        <div>
          <h4 class="text-xs font-semibold text-gray-700">
            代码定位
          </h4>
          <ol
            v-if="selected.frames.length"
            class="mt-2 bg-gray-50 border border-gray-100 rounded-xl p-3 space-y-2 text-xs font-mono text-gray-600"
          >
            <li
              v-for="(frame, index) in selected.frames"
              :key="`${index}:${frame.file}:${frame.line}`"
              class="break-all"
            >
              {{ frame.file }}:{{ frame.line }} · {{ frame.function }}
            </li>
          </ol>
          <p
            v-else
            class="mt-2 text-xs text-gray-400"
          >
            此类错误没有代码堆栈位置。
          </p>
        </div>
      </div>
    </Modal>
  </div>
</template>
