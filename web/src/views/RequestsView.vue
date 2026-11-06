<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Activity, RefreshCw, Search, Square, ArrowUpRight, Inbox } from 'lucide-vue-next'
import { useConsoleFeed } from '@/composables/useConsoleFeed'
import { cancelRequest, clock, duration, stageLabels, stateLabels, channelLabel, type RequestRecord, type CooldownRecord } from '@/api/console'
import { systemApi } from '@/api/system'
import { request } from '@/api/client'

const route = useRoute()
const router = useRouter()
const { items, error, loading, live, updated, refresh } = useConsoleFeed<RequestRecord>('/api/console/requests', 10000)
const cooldownFeed = useConsoleFeed<CooldownRecord>('/api/console/cooldowns', 10000)
watch(live, value => { cooldownFeed.live.value = value })
const cooldownModel = ref('')
const cooldownModels = computed(() => [...new Set(cooldownFeed.items.value.map(item => item.model))].sort())
const cooldowns = computed(() => cooldownFeed.items.value.filter(item => !cooldownModel.value || item.model === cooldownModel.value))
const clearing = ref('')
async function clearCooldown(item: CooldownRecord) {
  clearing.value = item.id; actionError.value = ''
  try { await systemApi.clearCooldown({ account_id: item.account_id, model: item.model === '*' ? undefined : item.model }); await cooldownFeed.refresh() }
  catch (cause) { actionError.value = cause instanceof Error ? cause.message : '清除冷却失败' }
  finally { clearing.value = '' }
}
async function refreshAll() { await Promise.all([refresh(), cooldownFeed.refresh()]) }
const filter = (name: string) => computed({
  get: () => typeof route.query[name] === 'string' ? route.query[name] as string : '',
  set: (value: string) => { void router.replace({ query: { ...route.query, [name]: value || undefined } }) },
})
const search = filter('q')
const model = filter('model')
const state = filter('state')
const selectedId = filter('id')
const models = computed(() => [...new Set(items.value.map(item => item.model).filter((value): value is string => !!value))].sort())
const visible = computed(() => items.value.filter(item => !['running', 'queued'].includes(item.state) &&
  (!model.value || item.model === model.value) && (!state.value || item.state === state.value) &&
  (!search.value || `${item.id} ${item.model ?? ''} ${item.protocol}`.toLowerCase().includes(search.value.toLowerCase())),
))
const historicalDetail = shallowRef<RequestRecord>()
const selected = computed(() => items.value.find(item => item.id === selectedId.value) ?? (historicalDetail.value?.id === selectedId.value ? historicalDetail.value : undefined))
watch(selectedId, async (id, _previous, cleanup) => {
  historicalDetail.value = undefined
  if (!id) return
  const controller = new AbortController()
  cleanup(() => controller.abort())
  try { historicalDetail.value = await request<RequestRecord>(`/api/console/requests/${encodeURIComponent(id)}`, { signal: controller.signal }) }
  catch { /* Recent records may still contain the live snapshot. */ }
}, { immediate: true })
const active = computed(() => items.value.filter(item => ['running', 'queued'].includes(item.state)).length)
const currentRequests = computed(() => items.value.filter(item => ['running', 'queued'].includes(item.state) && (!model.value || item.model === model.value)))
const failures = computed(() => items.value.filter(item => item.state === 'failed').length)
const tokenCount = computed(() => items.value.reduce((sum, item) => sum + (item.usage.total_tokens ?? 0), 0))
const cancelling = ref('')
const actionError = ref('')
async function cancel(item: RequestRecord) {
  cancelling.value = item.id
  actionError.value = ''
  try { await cancelRequest(item.id); await refresh() }
  catch (cause) { actionError.value = cause instanceof Error ? cause.message : '取消失败' }
  finally { cancelling.value = '' }
}
</script>

<template>
  <section class="monitor-page">
    <header class="monitor-heading">
      <div>
        <div class="eyebrow">
          OBSERVABILITY / REQUESTS
        </div><h1>请求管理</h1><p>查看当前请求与账号冷却，跟进每次生成的状态。</p>
      </div>
      <div class="feed-actions">
        <span
          class="feed-status"
          role="status"
        >{{ error ? '更新失败' : live ? '每 10 秒更新' : '已暂停' }}<small v-if="updated">{{ updated }}</small></span>
        <button
          type="button"
          class="console-button"
          :aria-pressed="live"
          @click="live = !live"
        >
          {{ live ? '暂停更新' : '继续更新' }}
        </button>
        <button
          type="button"
          class="console-button"
          :disabled="loading"
          @click="refreshAll"
        >
          <RefreshCw
            :size="15"
            :class="{ spinning: loading }"
          />刷新
        </button>
      </div>
    </header>
    <div class="monitor-metrics">
      <article><span>最近请求</span><strong>{{ items.length }}</strong><small>保留 7 天 · 展示最近 500 条</small></article>
      <article><span>进行中</span><strong class="accent-text">{{ active }}</strong><small>包含准备和上游生成</small></article>
      <article><span>失败请求</span><strong :class="{ 'error-text': failures }">{{ failures }}</strong><small>包含流内错误</small></article>
      <article><span>已报告 Tokens</span><strong>{{ tokenCount.toLocaleString() }}</strong><small>仅统计上游实际报告值</small></article>
    </div>
    <div
      v-if="error || actionError"
      class="console-error"
      role="alert"
    >
      {{ actionError || error }}<span v-if="error"> · 当前保留上次成功读取的数据</span>
    </div>
    <div class="request-live-grid">
      <article class="console-panel">
        <div class="panel-heading">
          <h2>冷却状态 <small>{{ cooldowns.length }}</small></h2><select
            v-model="cooldownModel"
            aria-label="冷却模型"
          >
            <option value="">
              全部模型
            </option><option
              v-for="item in cooldownModels"
              :key="item"
              :value="item"
            >
              {{ item === '*' ? '账号全局' : item }}
            </option>
          </select>
        </div>
        <p
          v-if="cooldownFeed.error.value"
          class="console-error"
          role="alert"
        >
          {{ cooldownFeed.error.value }}
        </p>
        <div class="live-list">
          <div
            v-for="item in cooldowns"
            :key="item.id"
            class="live-row"
          >
            <div><strong>{{ item.model === '*' ? '账号全局' : item.model }}</strong><small>{{ item.account_label }} · {{ item.reason }}</small></div><span class="cooldown-time">{{ Math.ceil(item.remaining / 60) }} 分钟<small>剩余 {{ item.remaining }} 秒</small></span><button
              type="button"
              class="console-button"
              :disabled="!!clearing"
              :aria-label="`清除冷却 ${item.id}`"
              @click="clearCooldown(item)"
            >
              解除
            </button>
          </div>
        </div>
        <div
          v-if="!cooldowns.length"
          class="console-empty compact-empty"
        >
          <h2>{{ cooldownFeed.loading.value ? '正在读取' : '没有冷却中的账号' }}</h2><p>发生限流后会显示模型与剩余时间。</p>
        </div>
      </article>
      <article class="console-panel">
        <div class="panel-heading">
          <h2>当前请求 <small class="accent-text">{{ active }} 个进行中</small></h2><RouterLink
            class="request-link"
            to="/usage"
          >用量统计 ↗</RouterLink>
        </div>
        <div class="live-list">
          <div
            v-for="item in currentRequests"
            :key="item.id"
            class="live-row"
          >
            <div>
              <button
                type="button"
                class="request-link"
                @click="selectedId = item.id"
              >
                {{ item.model ?? '准备请求' }}
              </button><small>{{ item.protocol }} · {{ channelLabel(item.channel) }} · {{ stateLabels[item.state] }} · {{ item.id.slice(-8) }}</small>
            </div><span class="tabular">{{ duration(item.duration_ms) }}</span><button
              type="button"
              class="stop-button"
              :disabled="!!cancelling"
              :aria-label="`停止请求 ${item.id}`"
              @click="cancel(item)"
            >
              <Square :size="12" />停止
            </button>
          </div>
        </div>
        <div
          v-if="!currentRequests.length"
          class="console-empty compact-empty"
        >
          <h2>暂无进行中的请求</h2><p>酒馆发起生成后自动显示，可在这里停止。</p>
        </div>
      </article>
    </div>
    <div class="request-workspace">
      <article class="console-panel">
        <div class="panel-heading">
          <h2>最近请求记录</h2><small>点击时间查看阶段详情</small>
        </div>
        <div class="console-toolbar">
          <label class="console-search"><Search :size="16" /><input
            v-model="search"
            aria-label="搜索请求 ID 或模型"
            placeholder="搜索请求 ID、模型、协议…"
          ></label>
          <select
            v-model="model"
            aria-label="筛选模型"
          >
            <option value="">
              全部模型
            </option><option
              v-for="item in models"
              :key="item"
            >
              {{ item }}
            </option>
          </select>
          <select
            v-model="state"
            aria-label="筛选请求状态"
          >
            <option value="">
              全部状态
            </option><option
              v-for="value in ['completed', 'failed', 'cancelled'] as const"
              :key="value"
              :value="value"
            >
              {{ stateLabels[value] }}
            </option>
          </select>
          <span class="muted-text">{{ visible.length }} 条</span>
        </div>
        <div class="request-table-scroll">
          <table class="request-table">
            <thead><tr><th>时间 / 请求</th><th>模型 / 协议</th><th>状态</th><th>总耗时</th><th>首正文</th><th>Tokens</th><th><span class="sr-only">操作</span></th></tr></thead>
            <tbody>
              <tr
                v-for="item in visible"
                :key="item.id"
                :class="{ selected: selectedId === item.id }"
              >
                <td>
                  <button
                    type="button"
                    class="request-link"
                    @click="selectedId = item.id"
                  >
                    {{ clock(item.started_at) }}<small>{{ item.id.slice(-8) }}</small>
                  </button>
                </td>
                <td><span class="model-name">{{ item.model ?? '未识别模型' }}</span><small>{{ item.protocol === 'openai' ? 'OpenAI' : 'Gemini' }} · {{ item.stream ? '流式' : '非流式' }}</small></td>
                <td>
                  <span
                    class="state-badge"
                    :data-state="item.state"
                  >{{ stateLabels[item.state] }}</span>
                </td>
                <td class="tabular">
                  {{ duration(item.duration_ms) }}
                </td><td class="tabular">
                  {{ duration(item.timings.first_body) }}
                </td>
                <td class="tabular">
                  {{ item.usage.total_tokens?.toLocaleString() ?? '未报告' }}
                </td>
                <td>
                  <button
                    v-if="['running', 'queued'].includes(item.state)"
                    type="button"
                    class="stop-button"
                    :disabled="!!cancelling"
                    :aria-label="`停止请求 ${item.id}`"
                    @click="cancel(item)"
                  >
                    <Square :size="12" />停止
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div
          v-if="!visible.length"
          class="console-empty"
        >
          <Inbox :size="32" /><h2>{{ loading ? '正在读取请求' : '暂无匹配的请求' }}</h2><p>酒馆发起请求后会自动出现在这里，也可以调整筛选条件。</p>
        </div>
      </article>
      <aside
        class="console-panel request-detail"
        aria-label="请求详情"
      >
        <template v-if="selected">
          <div class="detail-heading">
            <Activity :size="17" /><h2>请求详情</h2><span
              class="state-badge"
              :data-state="selected.state"
            >{{ stateLabels[selected.state] }}</span>
          </div>
          <code class="request-full-id">{{ selected.id }}</code>
          <dl class="detail-values">
            <div><dt>模型</dt><dd>{{ selected.model ?? '—' }}</dd></div>
            <div><dt>HTTP 状态</dt><dd>{{ selected.status || '等待响应' }}</dd></div>
            <div v-if="selected.error_status">
              <dt>错误状态</dt><dd class="error-text">
                {{ selected.error_status }}
              </dd>
            </div>
            <div><dt>总耗时 / 首正文</dt><dd>{{ duration(selected.duration_ms) }} / {{ duration(selected.timings.first_body) }}</dd></div>
            <div><dt>上游调用</dt><dd>{{ selected.attempts }} 次</dd></div>
            <div><dt>结束原因</dt><dd>{{ selected.finish_reason ?? '—' }}</dd></div>
            <div><dt>正文 / 思考</dt><dd>{{ selected.body_chars }} / {{ selected.thought_chars }} 字符</dd></div>
            <div><dt>输入 / 输出 Tokens</dt><dd>{{ selected.usage.prompt_tokens ?? '—' }} / {{ selected.usage.completion_tokens ?? '—' }}</dd></div>
          </dl>
          <h3 class="detail-subtitle">
            阶段时间线 <small>自接收请求起</small>
          </h3>
          <ol class="stage-timeline">
            <li
              v-for="(stage, index) in selected.stages"
              :key="index"
            >
              <span>{{ stageLabels[stage.event] ?? stage.event }}</span><time>{{ duration(stage.elapsed_ms) }}</time>
            </li>
          </ol>
          <RouterLink
            class="console-button detail-log-link"
            :to="{ path: '/logs', query: { q: selected.id } }"
          >查看关联日志<ArrowUpRight :size="15" /></RouterLink>
        </template>
        <div
          v-else
          class="console-empty"
        >
          <Activity :size="30" /><h2>{{ selectedId ? '记录已过期' : '选择一条请求' }}</h2><p>点击请求时间，查看阶段耗时和结束原因。</p>
        </div>
      </aside>
    </div>
    <p class="console-footnote">
      被动记录元数据，不保存对话、思考正文或凭据。首正文是服务端首次收到正文的时间。
    </p>
  </section>
</template>
