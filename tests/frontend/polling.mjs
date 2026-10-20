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
function clipboardHarness() {
  const timers = new Map(), unmounts = [], writes = [], successes = [], errors = []
  let sequence = 0
  const module = { exports: {} }
  const source = ts.transpileModule(fs.readFileSync(path.resolve(here, '../../web/src/composables/useClipboard.ts'), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  vm.runInNewContext(source, {
    module, exports: module.exports,
    require(name) {
      if (name === 'vue') return { ref: value => ({ value }), onUnmounted: fn => unmounts.push(fn) }
      assert.equal(name, '@/stores/toast.ts')
      return { useToastStore: () => ({ success: message => successes.push(message), error: message => errors.push(message) }) }
    },
    navigator: { clipboard: { writeText: text => new Promise((resolve, reject) => { writes.push({ text, resolve, reject }) }) } },
    setTimeout(fn) { const id = ++sequence; timers.set(id, fn); return id },
    clearTimeout(id) { timers.delete(id) },
  })
  return { use: module.exports.useClipboard, timers, unmounts, writes, successes, errors }
}

{
  const h = clipboardHarness()
  const first = h.use(), second = h.use()
  const firstCopy = first.copy('fixture-one', 'one')
  h.writes[0].resolve(); await firstCopy
  const secondCopy = second.copy('fixture-two', 'two')
  h.writes[1].resolve(); await secondCopy
  assert.equal(h.timers.size, 2, 'clipboard instances need independent feedback timers')
  for (const fn of [...h.timers.values()]) fn()
  assert.equal(first.copied.value, null)
  assert.equal(second.copied.value, null)
  checks.push('clipboard feedback expires independently for every mounted instance')
}
{
  const h = clipboardHarness()
  const clipboard = h.use()
  const copy = clipboard.copy('fixture-one', 'one')
  h.writes[0].resolve(); await copy
  assert.equal(h.timers.size, 1)
  h.unmounts[0]()
  assert.equal(h.timers.size, 0, 'unmount must clear clipboard feedback timers')
  const late = clipboard.copy('fixture-late', 'late')
  assert.equal(h.writes.length, 1, 'retained clipboard handlers must not run after unmount')
  await late
  checks.push('clipboard unmount clears timers and retires retained handlers')
}
{
  const h = clipboardHarness()
  const clipboard = h.use()
  const pending = clipboard.copy('fixture-pending', 'pending')
  h.unmounts[0]()
  h.writes[0].resolve(); await pending
  assert.equal(h.timers.size, 0)
  assert.equal(clipboard.copied.value, null)
  assert.equal(h.successes.length, 0)
  const other = h.use()
  const failed = other.copy('fixture-failed', 'failed')
  h.unmounts[1]()
  h.writes[1].reject(new Error('Synthetic clipboard rejection')); await failed
  assert.equal(h.errors.length, 0)
  checks.push('pending clipboard success and failure cannot update an unmounted component')
}
{
  const h = clipboardHarness()
  const clipboard = h.use()
  const older = clipboard.copy('fixture-old', 'old')
  const newer = clipboard.copy('fixture-new', 'new')
  h.writes[1].resolve(); await newer
  h.writes[0].resolve(); await older
  assert.equal(clipboard.copied.value, 'new')
  assert.equal(h.timers.size, 1)
  assert.equal(h.successes.length, 1)
  h.unmounts[0]()
  checks.push('out-of-order clipboard completion keeps feedback on the latest requested item')
}
const result = { ok: true, checks, groups: checks.length, modelInferenceInvoked: false }
console.log(JSON.stringify(result))
