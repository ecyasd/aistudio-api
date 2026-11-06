<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search, RefreshCw, ArrowUpRight, ScrollText, ChevronRight } from 'lucide-vue-next'
import { clock, duration, type LogRecord } from '@/api/console'
import { useConsoleFeed } from '@/composables/useConsoleFeed'

const route = useRoute()
const router = useRouter()
const { items, error, live, loading, updated, refresh } = useConsoleFeed<LogRecord>('/api/console/logs')
const filter = (name: string) => computed({
  get: () => typeof route.query[name] === 'string' ? route.query[name] as string : '',
  set: (value: string) => { void router.replace({ query: { ...route.query, [name]: value || undefined } }) },
})
const search = filter('q')
const level = filter('level')
const source = filter('source')
const follow = ref(true)
const output = ref<HTMLElement>()
const groups = computed(() => {
  const grouped = new Map<string, LogRecord[]>()
  for (const item of items.value) {
    const key = item.request_id ?? `service_${item.id}`
    const group = grouped.get(key) ?? []
    group.push(item)
    grouped.set(key, group)
  }
  return [...grouped.entries()].filter(([, events]) => events.some(item =>
    (!level.value || item.level === level.value) && (!source.value || item.source === source.value) &&
    (!search.value || `${item.request_id ?? ''} ${item.model ?? ''} ${item.message} ${item.event}`.toLowerCase().includes(search.value.toLowerCase())),
  )).map(([id, events]) => ({ id, events, latest: events[events.length - 1]! }))
})
watch([items, follow], async () => {
  await nextTick()
  if (follow.value && output.value) output.value.scrollTop = output.value.scrollHeight
})
</script>

<template>
  <section class="monitor-page">
    <header class="monitor-heading">
      <div>
        <div class="eyebrow">
          OBSERVABILITY / LOGS
        </div><h1>日志</h1><p>按请求归拢事件，沿着时间线定位问题。</p>
      </div>
      <div class="feed-actions">
        <span
          class="feed-status"
          role="status"
        >{{ error ? '更新失败' : live ? '每 2 秒更新' : '已暂停' }}<small v-if="updated">{{ updated }}</small></span><button
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
      v-if="error"
      role="alert"
      class="console-error"
    >
      {{ error }} · 当前保留上次成功读取的数据
    </div>
    <article class="console-panel log-workspace">
      <div class="console-toolbar">
        <label class="console-search"><Search :size="16" /><input
          v-model="search"
          aria-label="搜索日志"
          placeholder="搜索请求 ID、模型、事件…"
        ></label>
        <select
          v-model="level"
          aria-label="筛选日志级别"
        >
          <option value="">
            全部级别
          </option><option>INFO</option><option>WARN</option><option>ERROR</option>
        </select>
        <select
          v-model="source"
          aria-label="筛选日志来源"
        >
          <option value="">
            全部来源
          </option><option value="request">
            请求
          </option><option value="service">
            系统
          </option>
        </select>
        <label class="follow-control"><input
          v-model="follow"
          type="checkbox"
        >跟随最新</label>
      </div>
      <div class="log-column-head">
        <span>时间</span><span>级别</span><span>请求 / 事件</span><span>耗时</span>
      </div>
      <div
        ref="output"
        class="log-output"
        tabindex="0"
        aria-label="日志列表"
      >
        <details
          v-for="group in groups"
          :key="group.id"
          class="log-group"
        >
          <summary>
            <time>{{ clock(group.latest.time) }}</time><span
              class="log-level"
              :data-level="group.latest.level"
            >{{ group.latest.level }}</span><span class="log-summary"><ChevronRight :size="14" /><span>{{ group.latest.model ?? '系统' }}<small>{{ group.latest.message }} · {{ group.events.length }} 个事件<span v-if="group.latest.request_id"> · {{ group.latest.request_id.slice(-8) }}</span></small></span></span><span class="tabular">{{ group.latest.elapsed_ms === null ? '—' : duration(group.latest.elapsed_ms) }}</span>
          </summary>
          <div class="log-expanded">
            <div
              v-for="entry in group.events"
              :key="entry.id"
              class="log-event"
            >
              <time>{{ clock(entry.time) }}</time><span
                class="log-level"
                :data-level="entry.level"
              >{{ entry.level }}</span><span>{{ entry.message }}</span><span class="tabular">{{ entry.elapsed_ms === null ? '—' : duration(entry.elapsed_ms) }}</span>
            </div><RouterLink
              v-if="group.latest.request_id"
              class="log-request-link"
              :to="{ path: '/requests', query: { id: group.latest.request_id } }"
            >{{ group.latest.request_id }}<ArrowUpRight :size="14" /></RouterLink>
          </div>
        </details>
        <div
          v-if="!groups.length"
          class="console-empty"
        >
          <ScrollText :size="32" /><h2>{{ loading ? '正在读取日志' : '暂无匹配的日志' }}</h2><p>请求开始、重试、首字和结束事件会在这里按请求归组。</p>
        </div>
      </div>
      <footer class="log-footer">
        <span>{{ groups.length }} 组 / {{ items.length }} 个事件</span><span>内存保留 10 分钟 · 最多 1,000 个事件</span>
      </footer>
    </article>
  </section>
</template>
