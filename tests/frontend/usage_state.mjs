import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../web')
const require = createRequire(path.join(webRoot, 'package.json'))
const vue = require('vue')
const ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const pending = [], disposers = []
const source = compileScript(parse(fs.readFileSync(path.join(webRoot, 'src/views/UsageView.vue'), 'utf8')).descriptor, { id: 'usage-fixture' }).content
const javascript = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
}).outputText
const module = { exports: {} }
vm.runInNewContext(javascript, {
  module, exports: module.exports, console, AbortController, DOMException, Error,
  require(name) {
    if (name === 'vue') return { ...vue, onUnmounted: callback => disposers.push(callback) }
    if (name === '@/composables/usePolling.ts') return { usePolling() {} }
    if (name === '@/api/usage.ts') return { fetchUsage: (days, model, page, signal) => new Promise((resolve, reject) => pending.push({ days, model, page, signal, resolve, reject })) }
    if (name.endsWith('.vue')) return {}
    return require(name)
  },
}, { filename: 'UsageView.vue' })
const scope = vue.effectScope()
const state = scope.run(() => module.exports.default.setup({}, { expose() {} }))
const report = total => ({ total, items: [], models: [], totals: { requests: total }, retention: { days: 30, max_records: 1000 } })
const first = state.loadData()
state.days.value = 30
await vue.nextTick()
assert.equal(pending.length, 2)
assert.equal(pending[0].signal.aborted, true)
pending[1].resolve(report(2))
await vue.nextTick()
await vue.nextTick()
pending[0].resolve(report(1))
assert.equal(await first, false)
assert.equal(state.report.value.total, 2)
const failure = state.loadData()
pending[2].reject(new Error('synthetic read failure'))
assert.equal(await failure, false)
assert.equal(state.report.value.total, 2)
assert.equal(state.error.value, 'synthetic read failure')
const late = state.loadData()
disposers.forEach(dispose => dispose())
assert.equal(pending[3].signal.aborted, true)
pending[3].resolve(report(4))
assert.equal(await late, false)
assert.equal(state.report.value.total, 2)
scope.stop()
console.log(JSON.stringify({ passed: true, syntheticOnly: true, checks: ['filter changes abort old reads and reject late snapshots', 'read failure preserves the visible report and reports an error', 'unmount aborts pending reads and blocks later writes'] }))
