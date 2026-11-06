<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Activity, RefreshCw, Search, Square, ArrowUpRight, Inbox } from 'lucide-vue-next'
import { useConsoleFeed } from '@/composables/useConsoleFeed'
import { cancelRequest, clock, duration, stageLabels, stateLabels, type RequestRecord } from '@/api/console'

const route = useRoute()
const router = useRouter()
const { items, error, loading, live, updated, refresh } = useConsoleFeed<RequestRecord>('/api/console/requests')
const filter = (name: string) => computed({
  get: () => typeof route.query[name] === 'string' ? route.query[name] as string : '',
  set: (value: string) => { void router.replace({ query: { ...route.query, [name]: value || undefined } }) },
})
const search = filter('q')
const model = filter('model')
const state = filter('state')
const selectedId = filter('id')
const models = computed(() => [...new Set(items.value.map(item => item.model).filter((value): value is string => !!value))].sort())
const visible = computed(() => items.value.filter(item =>
  (!model.value || item.model === model.value) && (!state.value || item.state === state.value) &&
  (!search.value || `${item.id} ${item.model ?? ''} ${item.protocol}`.toLowerCase().includes(search.value.toLowerCase())),
))
const selected = computed(() => items.value.find(item => item.id === selectedId.value))
const active = computed(() => items.value.filter(item => ['running', 'queued'].includes(item.state)).length)
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
        </div><h1>请求</h1><p>从接收到结束，看清每一次生成。</p>
      </div>
      <div class="feed-actions">
        <span
          class="feed-status"
          role="status"
        >{{ error ? '更新失败' : live ? '每 2 秒更新' : '已暂停' }}<small v-if="updated">{{ updated }}</small></span>
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
          @click="refresh"
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
    <div class="request-workspace">
      <article class="console-panel">
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
              v-for="(label, value) in stateLabels"
              :key="value"
              :value="value"
            >
              {{ label }}
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
