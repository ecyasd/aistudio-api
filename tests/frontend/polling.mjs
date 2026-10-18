import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const require = createRequire(path.resolve(here, '../../web/package.json'))
const ts = require('typescript')
const code = ts.transpileModule(fs.readFileSync(path.resolve(here, '../../web/src/composables/usePolling.ts'), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }
const checks = []

function harness() {
  const timers = new Map(), unmounts = []
  let sequence = 0, visibility
  const document = { hidden: false, addEventListener(_, fn) { visibility = fn } }
  const module = { exports: {} }
  vm.runInNewContext(code, {
    module, exports: module.exports, console, document,
    require(name) { assert.equal(name, 'vue'); return { onUnmounted(fn) { unmounts.push(fn) } } },
    window: { location: { protocol: 'https:' }, setTimeout(fn) { const id = ++sequence; timers.set(id, fn); return id } },
    clearTimeout(id) { timers.delete(id) },
  })
  return { use: module.exports.usePolling, timers, unmounts, document, visibility: () => visibility() }
}

{
  const h = harness()
  let release, calls = 0
  const handle = h.use(async () => { calls++; await new Promise(resolve => { release = resolve }) }, 5000)
  assert.equal(calls, 1)
  h.unmounts[0]()
  release()
  await tick()
  handle.run(); handle.resume()
  await tick()
  assert.equal(calls, 1)
  assert.equal(h.timers.size, 0)
  checks.push('pending callbacks and retained handles cannot resurrect an unmounted poller')
}
{
  const h = harness()
  let release, calls = 0
  const handle = h.use(async () => { calls++; await new Promise(resolve => { release = resolve }) }, 5000)
  handle.pause()
  release(); await tick()
  handle.run(); await tick()
  assert.equal(calls, 1)
  assert.equal(h.timers.size, 0)
  handle.resume(); assert.equal(h.timers.size, 1)
  h.unmounts[0](); assert.equal(h.timers.size, 0)
  checks.push('pause persists through pending completion and resumes one timer')
}
{
  const h = harness()
  let release, oldCalls = 0, freshCalls = 0
  h.use(async () => { oldCalls++; await new Promise(resolve => { release = resolve }) }, 5000, { key: 'same' })
  h.use(() => { freshCalls++ }, 5000, { key: 'same' })
  await tick()
  h.unmounts[0](); release(); await tick()
  assert.equal(h.timers.size, 1)
  h.document.hidden = true; h.visibility(); assert.equal(h.timers.size, 0)
  h.document.hidden = false; h.visibility(); await tick()
  assert.equal(oldCalls, 1); assert.equal(freshCalls, 2); assert.equal(h.timers.size, 1)
  h.unmounts[1](); assert.equal(h.timers.size, 0)
  checks.push('replacing a keyed poller retires old work without removing the current visibility handler')
}
const result = { ok: true, checks, groups: checks.length, modelInferenceInvoked: false }
console.log(JSON.stringify(result))
