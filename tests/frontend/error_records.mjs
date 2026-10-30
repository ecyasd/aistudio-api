import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const workRoot = path.dirname(fileURLToPath(import.meta.url))
const webRoot = path.resolve(workRoot, '../../web')
const require = createRequire(path.resolve(workRoot, '../../web/package.json'))
const vue = require('vue')
const pinia = require('pinia')
const ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const { renderToString } = require('@vue/server-renderer')
const checks = []
const routeFixture = vue.reactive({ query: {} })
const routerFixture = { replace: async location => { routeFixture.query = location.query ?? {} } }
const RouterLink = vue.defineComponent({
  props: ['to'], setup: (props, { slots }) => () => {
    const target = props.to
    const query = typeof target === 'object' && target.query ? `?${new URLSearchParams(target.query)}` : ''
    return vue.h('a', { href: typeof target === 'string' ? target : `${target.path}${query}` }, slots.default?.())
  },
})
const pageRequests = []
const pollCallbacks = []
const pollIntervals = []
let clockMs = Date.now()
class FixtureDate extends Date {
  constructor(value) { super(arguments.length === 0 ? clockMs : value) }
  static now() { return clockMs }
}
let requestImpl = async () => { throw new Error('No fixture response') }
class ApiError extends Error { constructor(status) { super('synthetic request failure'); this.status = status } }
const storeCache = new Map()

function execute(source, filename, overrides = {}, globals = {}) {
  const javascript = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(javascript, {
    exports: module.exports, module, console, AbortController, URLSearchParams, Date: FixtureDate, ...globals,
    require(name) {
      if (name in overrides) return overrides[name]
      if (name === 'vue-router') return { RouterLink, useRoute: () => routeFixture, useRouter: () => routerFixture }
      if (name === '@/api/errors.ts') return { errorsApi: { list: async (query, signal) => {
        pageRequests.push({ ...query, signal })
        return requestImpl(query, signal)
      } } }
      if (name === '@/api/client.ts') return { ApiError }
      if (name === '@/composables/usePolling.ts') return { usePolling: (callback, interval) => { pollCallbacks.push(callback); pollIntervals.push(interval) } }
      if (name.endsWith('.vue')) return vue.defineComponent({ inheritAttrs: false, setup: (_, { slots }) => () => slots.default?.() })
      if (name.startsWith('@/')) return load(name.slice(2))
      return require(name)
    },
  }, { filename })
  return module.exports
}

function load(relative) {
  if (!storeCache.has(relative)) {
    storeCache.set(relative, execute(fs.readFileSync(path.join(webRoot, 'src', relative), 'utf8'), relative))
  }
  return storeCache.get(relative)
}

function freshStore() {
  pinia.setActivePinia(pinia.createPinia())
  return load('stores/errors.ts').useErrorsStore()
}

function record(overrides = {}) {
  return {
    id: 'synthetic-error-id', request_id: 'synthetic-request-id', time: new Date(clockMs).toISOString(),
    status: 503, transport_status: 200, category: 'browser', summary: '浏览器未就绪 <script>unsafe()</script>',
    exception_type: 'SyntheticBrowserError', frames: [{ file: 'gateway/session.py', line: 123, function: 'prepare' }],
    source: 'gemini_api', operation: 'streamGenerateContent', model: 'synthetic-model', account_id: 'synthetic-account',
    attempt: 1, retrying: false, outcome: 'failed', ...overrides,
  }
}

function diagnostic(overrides = {}) {
  return {
    request_id: 'synthetic-response-request', time: new Date(clockMs).toISOString(), model: 'synthetic-model',
    stream: true, auto_continue: false, outcome: 'success', transport_status: 200, duration_ms: 1250,
    upstream_calls: 1, retries: 0, finish_reasons: [1], body_chars: 420, thought_chars: 80,
    thought_after_body: 0, empty_thought_parts: 0, marker_count: 0,
    phase_sequence: ['thought', 'body', 'finish'], flags: [], error_kind: null, ...overrides,
  }
}

function response(items = [], overrides = {}) {
  return { items, total: items.length, has_more: false, next_cursor: null,
    retention: { max_records: 100, minutes: 10 },
    diagnostics: { items: [], retention: { max_records: 100, minutes: 10 } }, ...overrides }
}

let store = freshStore()
requestImpl = async () => { throw new ApiError(503) }
assert.equal(await store.refresh(), false)
assert.equal(store.result, null, 'failed initial load must not create an empty-success result')
assert.match(store.error, /503/)
checks.push('initial read failure remains an explicit error, not an empty list')

requestImpl = async () => response([record()])
assert.equal(await store.refresh(), true)
assert.equal(store.items.length, 1)
const savedResult = store.result
requestImpl = async () => { throw new Error('synthetic network failure') }
assert.equal(await store.reload(), false)
assert.equal(store.result, savedResult, 'refresh failures must preserve the last successful records')
assert.match(store.error, /无法读取/)
checks.push('refresh failure preserves previously visible diagnostics with an error state')
clockMs += 11 * 60_000
assert.equal(await store.reload(), false)
assert.equal(store.items.length, 0, 'expired cache must disappear even when the service cannot be read')
assert.match(store.error, /无法读取/)
checks.push('cached diagnostics expire after ten minutes even during read failures')

store = freshStore()
let resolveOld
requestImpl = () => new Promise(resolve => { resolveOld = resolve })
const oldRequest = store.refresh()
const oldSignal = pageRequests.at(-1).signal
store.categoryFilter = 'browser'
requestImpl = async query => response([record({ id: `new-${query.category}` })])
await store.refresh()
resolveOld(response([record({ id: 'stale-result' })]))
await oldRequest
assert.equal(oldSignal.aborted, true)
assert.equal(store.items[0].id, 'new-browser', 'superseded responses must not overwrite current filters')
assert.equal(store.matchesFilters, true)
checks.push('filter changes abort stale reads and ignore late responses')

store = freshStore()
requestImpl = async query => query.cursor
  ? response([record({ id: 'second-page' })], { total: 26 })
  : response([record({ id: 'first-page' })], { total: 26, has_more: true, next_cursor: 'first-page' })
await store.refresh()
await store.nextPage()
assert.equal(store.page, 2)
assert.equal(store.items[0].id, 'second-page')
await store.previousPage()
assert.equal(store.page, 1)
assert.equal(store.items[0].id, 'first-page')
requestImpl = async () => { throw new ApiError(503) }
await store.nextPage()
assert.equal(store.page, 1, 'failed navigation must not relabel old records as the new page')
checks.push('cursor pagination and previous-page navigation preserve page state on read failure')

store = freshStore()
store.statusFilter = '503'
store.categoryFilter = 'browser'
requestImpl = async query => response([], {
  total: 0, has_more: !query.cursor, next_cursor: query.cursor ? null : 'filtered-errors-next-page',
  diagnostics: {
    items: [diagnostic({ request_id: query.cursor ? 'latest-on-page-two' : 'latest-success' })],
    retention: { max_records: 100, minutes: 10 },
  },
})
await store.refresh()
assert.equal(store.items.length, 0)
assert.equal(store.diagnosticItems[0].outcome, 'success', 'successful responses must survive error-only filters')
assert.equal(pageRequests.at(-1).include_diagnostics, true)
await store.nextPage()
assert.equal(store.page, 2)
assert.equal(store.diagnosticItems[0].request_id, 'latest-on-page-two', 'response probes stay latest rather than following error cursors')
assert.equal(pageRequests.at(-1).include_diagnostics, true)
requestImpl = async () => { throw new Error('synthetic response probe read failure') }
await store.reload()
assert.equal(store.diagnosticItems.length, 1, 'failed reads preserve unexpired response probes')
clockMs += 11 * 60_000
await store.reload()
assert.equal(store.diagnosticItems.length, 0, 'response probe cache expires even when reads fail')
checks.push('response probes include successes, ignore error filters and pagination, and expire during read failures')

store = freshStore()
let resolveCancelled
requestImpl = () => new Promise(resolve => { resolveCancelled = resolve })
const cancelledRequest = store.refresh()
store.cancel()
resolveCancelled(response([record()]))
await cancelledRequest
assert.equal(store.result, null)
assert.equal(store.diagnosticItems.length, 0)
assert.equal(store.loading, false)
assert.equal(store.error, '')
checks.push('leaving the page aborts an active read without writing stale state')

let capturedUrl
const capturedSignal = new AbortController().signal
const errorsApi = execute(fs.readFileSync(path.join(webRoot, 'src/api/errors.ts'), 'utf8'), 'api/errors.ts', {
  './client.ts': { request: async (url, options) => { capturedUrl = url; assert.equal(options.signal, capturedSignal); return response() } },
}).errorsApi
await errorsApi.list({ status: 503, category: 'browser', cursor: 'id/with ?symbols', limit: 25, include_diagnostics: true }, capturedSignal)
const parsedUrl = new URL(capturedUrl, 'https://synthetic.invalid')
assert.equal(parsedUrl.pathname, '/errors')
assert.equal(parsedUrl.searchParams.get('status'), '503', 'the semantic error code must be used independently of HTTP transport')
assert.equal(parsedUrl.searchParams.get('cursor'), 'id/with ?symbols')
assert.equal(parsedUrl.searchParams.get('include_diagnostics'), 'true')
checks.push('API filters semantic error codes independently of HTTP transport and URL-encodes cursor values')
const malformedApi = execute(fs.readFileSync(path.join(webRoot, 'src/api/errors.ts'), 'utf8'), 'api/errors.ts', {
  './client.ts': { request: async () => ({}) },
}).errorsApi
await assert.rejects(malformedApi.list(), /响应格式无效/)
checks.push('malformed successful HTTP responses cannot be treated as an empty record list')
const missingDiagnosticsApi = execute(fs.readFileSync(path.join(webRoot, 'src/api/errors.ts'), 'utf8'), 'api/errors.ts', {
  './client.ts': { request: async () => ({ ...response(), diagnostics: undefined }) },
}).errorsApi
await assert.rejects(missingDiagnosticsApi.list({ include_diagnostics: true }), /响应探测记录格式无效/)
const malformedDiagnosticApi = execute(fs.readFileSync(path.join(webRoot, 'src/api/errors.ts'), 'utf8'), 'api/errors.ts', {
  './client.ts': { request: async () => response([], {
    diagnostics: { items: [diagnostic({ phase_sequence: 'invalid-phases' })], retention: { max_records: 100, minutes: 10 } },
  }) },
}).errorsApi
await assert.rejects(malformedDiagnosticApi.list({ include_diagnostics: true }), /响应探测记录格式无效/)
const nonStringOutcomeApi = execute(fs.readFileSync(path.join(webRoot, 'src/api/errors.ts'), 'utf8'), 'api/errors.ts', {
  './client.ts': { request: async () => response([], {
    diagnostics: { items: [diagnostic({ outcome: ['success'] })], retention: { max_records: 100, minutes: 10 } },
  }) },
}).errorsApi
await assert.rejects(nonStringOutcomeApi.list({ include_diagnostics: true }), /响应探测记录格式无效/)
checks.push('requested response diagnostics must be present and valid before a read succeeds')

for (const fields of [{ prompt_tokens: '80000' }, { request_body_bytes: -1 }, { timings_ms: { first_body: 'invalid' } }]) {
  const invalidMetricsApi = execute(fs.readFileSync(path.join(webRoot, 'src/api/errors.ts'), 'utf8'), 'api/errors.ts', {
    './client.ts': { request: async () => response([], { diagnostics: {
      items: [diagnostic(fields)], retention: { max_records: 100, minutes: 10 },
    } }) },
  }).errorsApi
  await assert.rejects(invalidMetricsApi.list({ include_diagnostics: true }), /响应探测记录格式无效/)
}
checks.push('new numeric diagnostic fields reject malformed counts and timings while older records remain compatible')

const viewPath = path.join(webRoot, 'src/views/ErrorRecordsView.vue')
const { descriptor } = parse(fs.readFileSync(viewPath, 'utf8'))
const view = execute(compileScript(descriptor, { id: 'error-records-regression', inlineTemplate: true }).content, viewPath).default
async function render() {
  return renderToString(vue.createSSRApp(view))
}

store = freshStore()
requestImpl = async () => { throw new ApiError(503) }
await store.refresh()
let html = await render()
assert.match(html, /读取错误记录失败/)
assert.doesNotMatch(html, /暂无实时错误/)
assert.match(html, /重试读取/)
checks.push('rendered initial failure offers retry and never says there are no errors')

requestImpl = async () => response()
await store.refresh()
html = await render()
assert.match(html, /暂无实时错误/)
assert.match(html, /服务重启后清空/)
assert.match(html, /过期记录自动清除/)
assert.match(html, /响应探测/)
assert.match(html, /暂无响应探测记录/)
assert.match(html, /独立于错误码及页码/)
assert.match(html, /本页显示最新 25 条/)
assert.match(html, /上游次数按开始重放的尝试计数/)
checks.push('a successful empty response explains the ten-minute live window and restart clearing')

requestImpl = async () => response([], {
  diagnostics: {
    items: [
      diagnostic({
        model: 'synthetic-model <script>probeUnsafe()</script>',
        phase_sequence: ['thought', 'body', 'thought', 'empty_thought', 'finish'],
        thought_after_body: 1, empty_thought_parts: 1, marker_count: 2,
        flags: ['marker_in_body', 'thought_after_body', 'empty_thought', 'content_after_finish', 'sequence_limited'],
        body_text: 'SYNTHETIC_RAW_BODY_MUST_NOT_RENDER', thought_text: 'SYNTHETIC_RAW_THOUGHT_MUST_NOT_RENDER',
        signature: 'SYNTHETIC_RAW_SIGNATURE_MUST_NOT_RENDER',
      }),
      diagnostic({
        request_id: 'synthetic-response-recovered', auto_continue: true, stream: false, outcome: 'recovered',
        upstream_calls: 3, retries: 1, finish_reasons: [2, 1],
      }),
      diagnostic({ request_id: 'synthetic-response-failed', outcome: 'failed', finish_reasons: [], error_kind: 'timeout' }),
      diagnostic({ request_id: 'synthetic-response-cancelled', outcome: 'cancelled', transport_status: null, finish_reasons: [99] }),
    ],
    retention: { max_records: 100, minutes: 10 },
  },
})
await store.refresh()
html = await render()
assert.match(html, /普通模式/)
assert.match(html, /抗截断/)
assert.match(html, /上游 3 次 · 重试 1 次/)
assert.match(html, /思考 → 正文 → 思考 → 空思考 \/ 签名 → 结束/)
assert.match(html, /正文后思考片段 1 个/)
assert.match(html, /正文含 \[:数字\] 标记 2 处/)
assert.match(html, /不直接判定为故障/)
assert.match(html, /结束信号后仍有内容/)
assert.match(html, /顺序记录已达上限/)
assert.match(html, /MAX_TOKENS/)
assert.match(html, /STOP/)
assert.match(html, /未收到结束原因/)
assert.match(html, /代码 99/)
assert.match(html, /synthetic-response-request/)
assert.match(html, /正文 420 字符 · 思考 80 字符/)
assert.match(html, /未发送响应/)
assert.match(html, /重试后成功/)
assert.match(html, /已取消/)
assert.match(html, /&lt;script&gt;probeUnsafe/)
assert.doesNotMatch(html, /<script>probeUnsafe|SYNTHETIC_RAW_(BODY|THOUGHT|SIGNATURE)_MUST_NOT_RENDER/)
checks.push('response probe rows show mode, attempts, phase order, observation signals and safe details for every outcome without raw content')

requestImpl = async () => response([
  record(),
  record({ id: 'recovered', outcome: 'recovered', retrying: true }),
  record({ id: 'warning', status: 503, transport_status: 200, outcome: 'warning' }),
])
await store.refresh()
html = await render()
assert.doesNotMatch(html, /持久化|保存到磁盘|保存状态/)
assert.match(html, /重试后成功/)
assert.match(html, /最终失败/)
assert.match(html, /警告/)
assert.match(html, /HTTP 200/)
assert.match(html, /synthetic-request-id/)
assert.doesNotMatch(html, /<script>unsafe/)
assert.match(html, /&lt;script&gt;/)
checks.push('live records distinguish recovery, final failure, warning and SSE transport without persistence UI; summaries are escaped')

const selectedRecord = record()
const detailsView = execute(compileScript(descriptor, { id: 'error-records-details', inlineTemplate: true }).content, viewPath, {
  vue: { ...vue, ref: value => vue.ref(value === null ? selectedRecord : value) },
}).default
html = await renderToString(vue.createSSRApp(detailsView))
assert.match(html, /gateway\/session.py:123/)
assert.match(html, /prepare/)
assert.match(html, /关联请求 ID/)
assert.match(html, /响应开始后的失败/)
assert.match(html, /SyntheticBrowserError/)
checks.push('details expose sanitized stack locations, request correlation and actual SSE HTTP status')

const identityRecord = record({
  status: 503, transport_status: 503, category: 'authentication',
  code: 'upstream_account_mismatch', summary: 'AI Studio 当前身份与所选账号不一致，已阻止请求。',
  exception_type: 'AccountIdentityMismatchError',
  frames: [{ file: 'aistudio_api/infrastructure/gateway/session_auth.py', line: 407, function: '_prepare_auth' }],
})
requestImpl = async () => response([identityRecord])
await store.refresh()
html = await render()
assert.match(html, /身份验证/)
assert.match(html, /身份与所选账号不一致/)
assert.match(html, /upstream_account_mismatch/)
const identityDetails = execute(compileScript(descriptor, { id: 'identity-error-details', inlineTemplate: true }).content, viewPath, {
  vue: { ...vue, ref: value => vue.ref(value === null ? identityRecord : value) },
}).default
html = await renderToString(vue.createSSRApp(identityDetails))
assert.match(html, /诊断代码/)
assert.match(html, /upstream_account_mismatch/)
assert.match(html, /AccountIdentityMismatchError/)
assert.match(html, /session_auth.py:407/)
checks.push('account identity diagnostics show a distinct safe code, authentication category and origin location')

const setupView = execute(compileScript(descriptor, { id: 'error-records-reactive-details' }).content, viewPath, {
  vue: { ...vue, onUnmounted() {} },
}).default
const viewScope = vue.effectScope()
const viewState = viewScope.run(() => setupView.setup({}, { expose() {} }))
viewState.selected.value = store.items[0]
requestImpl = async () => response([record({ outcome: 'recovered' })])
await store.refresh()
await vue.nextTick()
assert.equal(viewState.selected.value.outcome, 'recovered', 'open details must follow updated retry outcomes')
requestImpl = async () => response()
await store.refresh()
await vue.nextTick()
assert.equal(viewState.selected.value, null, 'removed or expired records must not remain open as current details')
assert.equal(viewState.detailsOpen.value, false)
viewScope.stop()
checks.push('open details follow retry recovery and close when the record expires or disappears')

assert(pollCallbacks.length > 0)
assert(pollIntervals.every(interval => interval === 5000), 'live error pages must poll every five seconds')
requestImpl = async () => { throw new Error('synthetic poll failure') }
await assert.rejects(pollCallbacks.at(-1), /轮询失败/)
assert.match(store.error, /无法读取/)
checks.push('poll failures remain visible and propagate to polling backoff')

let pollingCalls = 0
let visibilityChanged
let disposePolling
const timers = new Map()
const documentFixture = { hidden: false, addEventListener: (_, callback) => { visibilityChanged = callback } }
const polling = execute(fs.readFileSync(path.join(webRoot, 'src/composables/usePolling.ts'), 'utf8'), 'composables/usePolling.ts', {
  vue: { onUnmounted: callback => { disposePolling = callback } },
}, {
  window: { location: { protocol: 'https:' }, setTimeout: (callback, delay) => { const id = timers.size + 1; timers.set(id, { callback, delay }); return id } },
  document: documentFixture,
  clearTimeout: id => timers.delete(id),
}).usePolling
polling(async () => { pollingCalls++ }, 5000)
await new Promise(resolve => setImmediate(resolve))
assert.equal(pollingCalls, 1)
assert.equal([...timers.values()][0].delay, 5000)
documentFixture.hidden = true
visibilityChanged()
assert.equal(timers.size, 0)
documentFixture.hidden = false
visibilityChanged()
await new Promise(resolve => setImmediate(resolve))
assert.equal(pollingCalls, 2)
disposePolling()
assert.equal(timers.size, 0)
checks.push('the shared polling scheduler stops while hidden, refreshes on visibility and disposes timers')

store = freshStore()
requestImpl = async () => response([], { diagnostics: { items: [diagnostic({
  prompt_tokens: 80_000, request_body_bytes: 320_000,
  session_preparation_reason: 'warm_reuse', browser_idle_timeout_s: 1800,
  timings_ms: { account_preparation_started: 200, account_ready: 7040,
    transport_dispatched: 7100, upstream_first_byte: 30600, first_body: 30601, first_thought: 0,
    browser_launch_started: 200, browser_ready: 900, SYNTHETIC_PRIVATE_TIMING: 123 },
  flags: ['continuation_skipped_budget', 'continuation_skipped_unsupported'],
}), diagnostic({ request_id: 'synthetic-unknown-size', prompt_tokens: null,
  session_preparation_reason: 'SYNTHETIC_PRIVATE_REASON', browser_idle_timeout_s: 0 })],
retention: { max_records: 100, minutes: 10 } } })
await store.refresh()
html = await render()
for (const text of ['80,000', '312.5 KiB', '6.840 秒', '23.500 秒', '30.601 秒', '0.000 秒', '0.900 秒',
  '历史预算不足', '暂不支持回放', '输入 未知', '未记录']) assert(html.includes(text), text)
for (const text of ['复用热会话', '1800 秒', '不自动回收']) assert(html.includes(text), text)
assert.doesNotMatch(html, /SYNTHETIC_PRIVATE_TIMING|SYNTHETIC_PRIVATE_REASON/)
checks.push('latency rows distinguish local preparation, upstream wait, absolute first content, zero and unknown without rendering unrecognized metadata')

const { millisecondsUntilPacificMidnight } = load('utils/format.ts')
for (const [stamp, hours] of [
  ['2026-01-01T08:00:00Z', 24], ['2026-07-01T07:00:00Z', 24],
  ['2026-03-08T08:00:00Z', 23], ['2026-11-01T07:00:00Z', 25],
  ['2026-07-01T06:30:00Z', 0.5],
]) assert.equal(millisecondsUntilPacificMidnight(new Date(stamp)), hours * 3_600_000, stamp)
checks.push('Pacific quota countdown uses actual winter/summer offsets and 23/25-hour daylight-saving transition days')

store = freshStore()
const contextRecord = record({ account_id: 'acc_11111111', request_id: 'req_11111111' })
const contextDiagnostic = diagnostic({ account_ids: ['acc_11111111', 'acc_22222222'], request_id: 'req_11111111',
  transport_wait_phase: 'awaiting_ack', transport_probes: 2, transport_repair_attempts: 1 })
requestImpl = async () => response([contextRecord], { diagnostics: { items: [contextDiagnostic], retention: { minutes: 10, max_records: 100 } } })
await store.refresh()
store.accountFilter = 'acc_33333333'
assert.equal(store.items.length, 0)
assert.equal(store.diagnosticItems.length, 0)
assert.equal(store.matchesFilters, false)
requestImpl = async () => { throw new Error('synthetic context refresh failure') }
await store.refresh()
assert.equal(store.items.length, 0, 'failed context navigation cannot show another account as the current account')
checks.push('account context changes hide unrelated cached errors and diagnostics even when the new read fails')

const pendingContext = []
requestImpl = (query, signal) => new Promise(resolve => pendingContext.push({ query, signal, resolve }))
const oldContextRead = store.refresh()
store.accountFilter = 'acc_11111111'
store.requestFilter = 'req_11111111'
const newContextRead = store.refresh()
assert.equal(pendingContext[0].signal.aborted, true)
pendingContext[1].resolve(response([contextRecord]))
await newContextRead
pendingContext[0].resolve(response([record({ account_id: 'acc_33333333' })]))
await oldContextRead
assert.equal(store.items[0].account_id, 'acc_11111111')
assert.equal(pendingContext[1].query.account_id, 'acc_11111111')
assert.equal(pendingContext[1].query.request_id, 'req_11111111')
assert.equal(store.page, 1)
checks.push('account and request filters travel together and older responses cannot replace a newer context')

routeFixture.query = { account_id: 'acc_11111111' }
requestImpl = async () => response([contextRecord], { diagnostics: { items: [contextDiagnostic], retention: { minutes: 10, max_records: 100 } } })
await store.refresh()
html = await render()
assert.match(html, /账号范围/)
assert.match(html, /\/accounts\?account_id=acc_11111111/)
assert.match(html, /\/error-records\?request_id=req_11111111/)
assert.match(html, /等待片段确认/)
assert.match(html, /恢复尝试 1 次/)
const routeScope = vue.effectScope()
routeScope.run(() => setupView.setup({}, { expose() {} }))
routeFixture.query = { request_id: 'req_11111111' }
await vue.nextTick()
assert.equal(store.accountFilter, '', 'request-chain links must include attempts using other accounts')
assert.equal(store.requestFilter, 'req_11111111')
routeScope.stop()
routeFixture.query = {}
checks.push('account and request links preserve exact identities and request navigation clears the account restriction')

const contextParser = load('utils/diagnosticContext.ts').diagnosticContext
assert.equal(contextParser({ account_id: ['acc_11111111'], request_id: 'unsafe-request' }).account, '')
assert.equal(contextParser({ account_id: 'acc_11111111' }).account, 'acc_11111111')

const result = { passed: true, checks, groups: checks.length, syntheticOnly: true, upstreamModelCalls: false }
console.log(JSON.stringify(result))
