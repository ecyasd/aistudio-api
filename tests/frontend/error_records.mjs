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

function response(items = [], overrides = {}) {
  return { items, total: items.length, has_more: false, next_cursor: null,
    retention: { max_records: 100, minutes: 10 }, ...overrides }
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
let resolveCancelled
requestImpl = () => new Promise(resolve => { resolveCancelled = resolve })
const cancelledRequest = store.refresh()
store.cancel()
resolveCancelled(response([record()]))
await cancelledRequest
assert.equal(store.result, null)
assert.equal(store.loading, false)
assert.equal(store.error, '')
checks.push('leaving the page aborts an active read without writing stale state')

let capturedUrl
const capturedSignal = new AbortController().signal
const errorsApi = execute(fs.readFileSync(path.join(webRoot, 'src/api/errors.ts'), 'utf8'), 'api/errors.ts', {
  './client.ts': { request: async (url, options) => { capturedUrl = url; assert.equal(options.signal, capturedSignal); return response() } },
}).errorsApi
await errorsApi.list({ status: 503, category: 'browser', cursor: 'id/with ?symbols', limit: 25 }, capturedSignal)
const parsedUrl = new URL(capturedUrl, 'https://synthetic.invalid')
assert.equal(parsedUrl.pathname, '/errors')
assert.equal(parsedUrl.searchParams.get('status'), '503', 'the semantic error code must be used independently of HTTP transport')
assert.equal(parsedUrl.searchParams.get('cursor'), 'id/with ?symbols')
checks.push('API filters semantic error codes independently of HTTP transport and URL-encodes cursor values')
const malformedApi = execute(fs.readFileSync(path.join(webRoot, 'src/api/errors.ts'), 'utf8'), 'api/errors.ts', {
  './client.ts': { request: async () => ({}) },
}).errorsApi
await assert.rejects(malformedApi.list(), /响应格式无效/)
checks.push('malformed successful HTTP responses cannot be treated as an empty record list')

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
checks.push('a successful empty response explains the ten-minute live window and restart clearing')

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

const result = { passed: true, checks, groups: checks.length, syntheticOnly: true, upstreamModelCalls: false }
console.log(JSON.stringify(result))
