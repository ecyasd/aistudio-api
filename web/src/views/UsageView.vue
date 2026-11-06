<script setup lang="ts">
import { computed, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { RefreshCw, BarChart3, ChevronLeft, ChevronRight } from 'lucide-vue-next'
import { getUsage, type UsageReport, type UsageStats } from '@/api/usage'
import { clock, duration, stateLabels, channelLabel, engineLabel } from '@/api/console'
import { usePolling } from '@/composables/usePolling'
import UsageCharts from '@/components/console/UsageCharts.vue'

const route = useRoute()
const router = useRouter()
const filter = (name: string, fallback = '') => computed({
  get: () => typeof route.query[name] === 'string' ? route.query[name] as string : fallback,
  set: (value: string) => { void router.replace({ query: { ...route.query, [name]: value || undefined, page: undefined } }) },
})
const range = filter('range', '24h'), model = filter('model'), protocol = filter('protocol'), state = filter('state'), channel = filter('channel')
const from = filter('from'), to = filter('to')
const page = computed(() => Math.max(1, Number(route.query.page) || 1))
const report = shallowRef<UsageReport>()
const error = ref(''), loading = ref(false), updated = ref(''), live = ref(true)
let controller: AbortController | undefined
let disposed = false
async function refresh() {
  controller?.abort()
  const current = new AbortController(); controller = current
  const end = range.value === 'custom' ? new Date(to.value).getTime() / 1000 : Date.now() / 1000
  const start = range.value === 'custom' ? new Date(from.value).getTime() / 1000 : end - ({ '1h': 3600, '24h': 86400, '7d': 604800 }[range.value] ?? 86400)
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || end - start > 31 * 86400) { error.value = '请选择有效的开始和结束时间，范围不超过 31 天'; loading.value = false; return }
  const query = new URLSearchParams({ from: String(start), to: String(end), model: model.value, protocol: protocol.value, state: state.value, channel: channel.value, page: String(page.value) })
  loading.value = true
  try {
    const value = await getUsage(query, current.signal)
    if (disposed || current.signal.aborted) return
    report.value = value; error.value = ''; updated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (cause) { if (!disposed && !current.signal.aborted) error.value = cause instanceof Error ? cause.message : '用量读取失败' }
  finally { if (!disposed && controller === current) loading.value = false }
}
const polling = usePolling(refresh, 30000)
watch(live, value => { if (value) { polling.resume(); void refresh() } else polling.pause() })
watch(() => route.fullPath, refresh)
onUnmounted(() => { disposed = true; controller?.abort() })
const number = (value: number) => value.toLocaleString('zh-CN')
const rate = (stats: UsageStats) => stats.requests - stats.cancelled > 0 ? `${(stats.success / (stats.requests - stats.cancelled) * 100).toFixed(1)}%` : '—'
const delta = (key: 'requests' | 'total_tokens') => {
  if (!report.value || !report.value.previous[key]) return '上期无数据'
  const change = (report.value.totals[key] / report.value.previous[key] - 1) * 100
  return `较上期 ${change >= 0 ? '+' : ''}${change.toFixed(1)}%`
}
function changePage(value: number) { void router.replace({ query: { ...route.query, page: String(value) } }) }
function reset() { void router.replace({ query: { range: range.value, from: from.value || undefined, to: to.value || undefined } }) }
</script>

<template>
  <section class="monitor-page usage-page">
    <header class="monitor-heading">
      <div>
        <div class="eyebrow">
          OBSERVABILITY / USAGE
        </div><h1>用量统计</h1><p>查看请求趋势、成功率、Token 使用和首正文耗时。</p>
      </div><div class="feed-actions">
        <span
          class="feed-status"
          role="status"
        >{{ error ? '更新失败' : live ? '每 30 秒更新' : '已暂停' }}<small>{{ updated }}</small></span><button
          type="button"
          class="console-button"
          :aria-pressed="live"
          @click="live = !live"
        >
          {{ live ? '暂停更新' : '继续更新' }}
        </button><button
          type="button"
          class="console-button"
          :disabled="loading"
          @click="refresh"
        >
          <RefreshCw
            :size="15"
            :class="{ spinning: loading }"
          />刷新
        </button>
      </div>
    </header>
    <div
      class="console-panel console-toolbar usage-filters"
      role="toolbar"
      aria-label="用量筛选"
    >
      <select
        v-model="range"
        aria-label="统计时间范围"
      >
        <option value="1h">
          最近 1 小时
        </option><option value="24h">
          最近 24 小时
        </option><option value="7d">
          最近 7 天
        </option><option value="custom">
          自定义时间
        </option>
      </select>
      <template v-if="range === 'custom'">
        <label>从 <input
          v-model="from"
          type="datetime-local"
          aria-label="开始时间"
        ></label><label>至 <input
          v-model="to"
          type="datetime-local"
          aria-label="结束时间"
        ></label>
      </template>
      <select
        v-model="model"
        aria-label="用量模型"
      >
        <option value="">
          全部模型
        </option><option
          v-for="item in report?.options.models ?? []"
          :key="item"
        >
          {{ item }}
        </option>
      </select>
      <select
        v-model="protocol"
        aria-label="用量协议"
      >
        <option value="">
          全部协议
        </option><option value="openai">
          OpenAI
        </option><option value="gemini">
          Gemini
        </option>
      </select>
      <select
        v-model="channel"
        aria-label="用量通道"
      >
        <option value="">
          全部通道
        </option><option value="playground">
          PG
        </option><option value="assistant">
          Build 代码助手
        </option>
      </select>
      <select
        v-model="state"
        aria-label="用量状态"
      >
        <option value="">
          全部结果
        </option><option value="completed">
          成功
        </option><option value="failed">
          失败
        </option><option value="cancelled">
          已取消
        </option>
      </select>
      <button
        v-if="model || protocol || state || channel"
        type="button"
        class="console-button"
        @click="reset"
      >
        清除筛选
      </button>
    </div>
    <div
      v-if="error"
      class="console-error"
      role="alert"
    >
      {{ error }}<span v-if="report"> · 保留上次成功读取的数据</span>
    </div>
    <template v-if="report">
      <p class="usage-scope muted-text">
        {{ new Date(report.from * 1000).toLocaleString('zh-CN') }} — {{ new Date(report.to * 1000).toLocaleString('zh-CN') }} · 已结束请求 · {{ Intl.DateTimeFormat().resolvedOptions().timeZone }}
      </p>
      <div class="monitor-metrics usage-metrics">
        <article><span>总请求</span><strong>{{ number(report.totals.requests) }}</strong><small>{{ delta('requests') }} · 取消 {{ report.totals.cancelled }}</small></article>
        <article><span>成功率</span><strong class="accent-text">{{ rate(report.totals) }}</strong><small>失败 {{ report.totals.failed }} · 429 限流 {{ report.totals.rate_limited }} · 不含取消</small></article>
        <article><span>已报告 Tokens</span><strong>{{ number(report.totals.total_tokens) }}</strong><small>{{ delta('total_tokens') }} · {{ report.totals.reported_usage }}/{{ report.totals.requests }} 条有报告</small></article>
        <article><span>输入 / 输出 Tokens</span><strong>{{ number(report.totals.input_tokens) }} <small>/ {{ number(report.totals.output_tokens) }}</small></strong><small>缓存命中 {{ number(report.totals.cached_tokens) }}</small></article>
        <article><span>流式首正文 P50</span><strong>{{ duration(report.totals.first_body_p50 ?? undefined) }}</strong><small>P95 {{ duration(report.totals.first_body_p95 ?? undefined) }}</small></article>
        <article><span>请求耗时 P50</span><strong>{{ duration(report.totals.duration_p50 ?? undefined) }}</strong><small>P95 {{ duration(report.totals.duration_p95 ?? undefined) }}</small></article>
      </div>
      <div
        v-if="!report.totals.requests"
        class="console-panel console-empty"
      >
        <BarChart3 :size="32" /><h2>当前范围没有请求记录</h2><p>酒馆调用后会自动统计，也可以调整时间和筛选条件。</p>
      </div>
      <UsageCharts :report="report" />
      <article class="console-panel usage-ranking">
        <div class="panel-heading">
          <h2>模型排行</h2><small>点击模型查看明细</small>
        </div><div class="request-table-scroll">
          <table class="request-table">
            <thead><tr><th>模型</th><th>请求</th><th>成功率</th><th>Tokens</th><th>首正文 P50</th><th>耗时 P95</th></tr></thead><tbody>
              <tr
                v-for="item in report.ranking"
                :key="item.model"
              >
                <td>
                  <button
                    type="button"
                    class="request-link"
                    @click="model = item.model"
                  >
                    {{ item.model }}
                  </button>
                </td><td>{{ number(item.requests) }}</td><td>{{ rate(item) }}</td><td>{{ number(item.total_tokens) }}<small>{{ item.reported_usage }} 条报告</small></td><td>{{ duration(item.first_body_p50 ?? undefined) }}</td><td>{{ duration(item.duration_p95 ?? undefined) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>
      <article class="console-panel">
        <div class="panel-heading">
          <h2>请求记录</h2><small>{{ report.totals.requests }} 条</small>
        </div><div class="request-table-scroll">
          <table class="request-table">
            <thead><tr><th>时间 / 请求</th><th>模型 / 通道</th><th>结果</th><th>Tokens</th><th>耗时</th><th>详情</th></tr></thead><tbody>
              <tr
                v-for="item in report.records"
                :key="item.id"
              >
                <td>{{ clock(item.started_at) }}<small>{{ item.id.slice(-8) }}</small></td><td>{{ item.model ?? '未识别模型' }}<small>{{ item.protocol }} · {{ channelLabel(item.channel) }} · {{ engineLabel(item.engine) }}</small></td><td>
                  <span
                    class="state-badge"
                    :data-state="item.state"
                  >{{ stateLabels[item.state] }}</span>
                </td><td>{{ item.usage.total_tokens === undefined ? '未报告' : number(item.usage.total_tokens) }}</td><td>{{ duration(item.duration_ms) }}</td><td>
                  <RouterLink
                    class="request-link"
                    :to="{ path: '/requests', query: { id: item.id } }"
                  >查看请求</RouterLink>
                </td>
              </tr>
            </tbody>
          </table>
        </div><div class="usage-pager">
          <button
            type="button"
            class="console-button"
            :disabled="report.page <= 1 || loading"
            aria-label="上一页"
            @click="changePage(report.page - 1)"
          >
            <ChevronLeft :size="16" />
          </button><span>{{ report.page }} / {{ report.pages }}</span><button
            type="button"
            class="console-button"
            :disabled="report.page >= report.pages || loading"
            aria-label="下一页"
            @click="changePage(report.page + 1)"
          >
            <ChevronRight :size="16" />
          </button>
        </div>
      </article>
      <p class="console-footnote">
        默认保留 {{ report.retention_days }} 天、最多 {{ number(report.max_records) }} 条结束记录；达到上限后按实际留存统计，上一周期也仅比较留存数据。未报告的 Tokens 不估算；首正文仅统计流式请求。费用无可靠价格来源，暂不估算。
      </p>
    </template>
    <div
      v-else-if="loading"
      class="console-panel console-empty"
      role="status"
    >
      正在读取用量统计…
    </div>
  </section>
</template>
