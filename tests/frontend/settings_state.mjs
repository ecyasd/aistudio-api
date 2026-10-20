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
const YAML = require('yaml')
const { parse, compileScript } = require('@vue/compiler-sfc')
const checks = []
function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
const toast = { success() {}, error() {}, info() {} }

function load(relative, imports = {}, globals = {}, component = false) {
  let source = fs.readFileSync(path.join(webRoot, 'src', relative), 'utf8')
  if (component) source = compileScript(parse(source).descriptor, { id: 'settings-state' }).content
  const javascript = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(javascript, {
    module, exports: module.exports, console, AbortController, confirm: () => true, ...globals,
    require(name) {
      if (name in imports) return imports[name]
      if (name === 'pinia') return { defineStore: (_name, setup) => setup }
      if (name === '@/stores/toast.ts' || name === './toast.ts') return { useToastStore: () => toast }
      if (name.endsWith('.vue')) return {}
      return require(name)
    },
  }, { filename: relative })
  return module.exports
}

function setup(relative, api, props = {}) {
  const callbacks = [], polls = [], events = [], scope = vue.effectScope()
  const exports = load(relative, {
    vue: { ...vue, onMounted() {}, onUnmounted: callback => callbacks.push(callback) },
    '@/api/system.ts': { systemApi: api },
    '@/composables/useClipboard.ts': { useClipboard: () => ({ copy() {}, copied: vue.ref('') }) },
    '@/composables/usePolling.ts': { usePolling: callback => polls.push(callback) },
    'vue-router': { onBeforeRouteLeave() {} },
  }, { setInterval() { return 1 }, clearInterval() {} }, true)
  const state = scope.run(() => exports.default.setup(props, { expose() {}, emit: (...event) => events.push(event) }))
  return { state, polls, events, stop() { callbacks.forEach(callback => callback()); scope.stop() } }
}

for (const [method, apiMethod, stateKey] of [['fetchStats', 'getStats', 'stats'], ['fetchRotation', 'getRotation', 'rotation'], ['checkHealth', 'health', 'health']]) {
  const requests = []
  const store = load('stores/system.ts', {
    '@/api/system.ts': { systemApi: { [apiMethod]: () => { const request = deferred(); requests.push(request); return request.promise } } },
  }).useSystemStore()
  const older = store[method](), current = store[method]()
  requests[1].resolve({ marker: 'current' })
  await current
  if (method === 'checkHealth') requests[0].reject(new Error('Synthetic stale failure'))
  else requests[0].resolve({ marker: 'old' })
  await older
  assert.equal(stateKey === 'health' ? store.health.value : store[stateKey].value.marker,
    stateKey === 'health' ? 'online' : 'current', `${method} must ignore an older completion`)
}
checks.push('older statistics, rotation and health reads cannot replace a newer result')

{
  const pending = []
  const store = load('stores/system.ts', {
    '@/api/system.ts': { systemApi: { getStats: () => { const request = deferred(); pending.push(request); return request.promise } } },
  }).useSystemStore()
  const older = store.fetchStats(), current = store.fetchStats()
  pending[0].reject(new Error('Synthetic stale failure')); await older
  assert.equal(store.loading.value, true, 'a stale completion must not clear the current loading state')
  pending[1].resolve({ marker: 'retained' }); await current
  assert.equal(store.loading.value, false)
  const failing = store.fetchStats()
  pending[2].reject(new Error('Synthetic current failure')); await failing
  assert.equal(store.stats.value.marker, 'retained', 'a failed refresh must preserve the latest successful snapshot')
  const retry = store.fetchStats()
  pending[3].resolve({ marker: 'retry' }); await retry
  assert.equal(store.stats.value.marker, 'retry')
}
checks.push('statistics loading follows the newest read and refresh failures preserve data and allow retry')

for (const action of ['clearCooldown', 'forceNextAccount']) {
  const pending = []
  const store = load('stores/system.ts', {
    '@/api/system.ts': { systemApi: {
      getRotation: () => { const request = deferred(); pending.push(request); return request.promise },
      [action]: async () => ({ account: { id: 'fixture-current-account' } }),
    } },
  }).useSystemStore()
  const older = store.fetchRotation()
  const mutation = store[action]()
  for (let i = 0; i < 8; i++) await Promise.resolve()
  pending[1].resolve({ marker: 'after-mutation' }); await mutation
  pending[0].resolve({ marker: 'before-mutation' }); await older
  assert.equal(store.rotation.value.marker, 'after-mutation', `${action} must retain its refreshed rotation snapshot`)
}
checks.push('cooldown reset and forced rotation retain the refreshed state after older polling finishes')

{
  const lists = [], actives = []
  const api = Object.fromEntries([['list', lists], ['getActive', actives]].map(([method, pending]) => [method, () => {
    const request = deferred(); pending.push(request); return request.promise
  }]))
  const store = load('stores/accounts.ts', {
    '@/api/accounts.ts': { accountsApi: api },
    './system.ts': { useSystemStore: () => ({ fetchRotation: async () => {}, rotation: null }) },
  }).useAccountsStore()
  const older = store.fetchAll(), current = store.fetchAll()
  lists[1].resolve([{ id: 'current-account' }]); actives[1].resolve({ id: 'current-account' })
  await current
  lists[0].resolve([{ id: 'deleted-account' }]); actives[0].resolve({ id: 'deleted-account' })
  await older
  assert.equal(store.accounts.value[0].id, 'current-account')
  assert.equal(store.activeId.value, 'current-account')
  const failedActive = store.fetchAll()
  lists[2].resolve([{ id: 'different-account' }]); actives[2].reject(new Error('Synthetic active failure'))
  await failedActive
  assert.equal(store.activeId.value, '', 'a removed account must not remain active when its fresh lookup fails')
}
checks.push('account refreshes ignore old snapshots and never invent an active account on lookup failure')

for (const [method, stateKey] of [['loadConfig', 'config'], ['loadBrowserStatus', 'browser']]) {
  const pending = []
  const h = setup('views/SettingsView.vue', {
    [method === 'loadConfig' ? 'getConfig' : 'getBrowserStatus']: signal => {
      const request = deferred(); pending.push({ signal, ...request }); return request.promise
    },
  })
  const older = h.state[method](), current = h.state[method]()
  pending[1].resolve({ marker: 'current' }); await current
  pending[0].resolve({ marker: 'old' }); await older
  assert.equal(h.state[stateKey].value.marker, 'current', `${method} must ignore an older response`)
  assert.equal(pending[0].signal.aborted, true)
  const leaving = h.state[method]()
  h.stop()
  pending[2].resolve({ marker: 'unmounted' }); await leaving
  assert.equal(pending[2].signal.aborted, true)
  assert.equal(h.state[stateKey].value.marker, 'current', `${method} must ignore unmounted completion`)
}
checks.push('settings supersede and abort stale reads and reject results after unmount')

{
  const signal = new AbortController().signal
  const calls = []
  const api = load('api/system.ts', { './client.ts': { request: async (url, options) => { calls.push({ url, options }) } } }).systemApi
  await api.getConfig(signal); await api.getBrowserStatus(signal); await api.listApiKeys(signal)
  assert.deepEqual(calls.map(call => call.url), ['/config', '/browser/status', '/api-keys'])
  assert(calls.every(call => call.options.signal === signal), 'read cancellation must reach the shared HTTP client')
}
checks.push('settings and key-list APIs pass cancellation through to the HTTP client')

{
  const pending = [], creates = [], edits = []
  const h = setup('components/settings/ApiKeyManagerCard.vue', {
    listApiKeys: signal => { const request = deferred(); pending.push({ signal, ...request }); return request.promise },
    createApiKey: payload => { const request = deferred(); creates.push({ payload, ...request }); return request.promise },
    updateApiKeyName: (key, payload) => { const request = deferred(); edits.push({ key, payload, ...request }); return request.promise },
  })
  const older = h.state.loadApiKeys(), current = h.state.loadApiKeys()
  pending[1].resolve([{ key: 'fixture-current', name: 'Current' }]); await current
  pending[0].resolve([{ key: 'fixture-deleted', name: 'Deleted' }]); await older
  assert.equal(h.state.apiKeys.value[0].key, 'fixture-current', 'old lists must not restore a deleted key')
  h.state.openCreateModal(); h.state.newKeyName.value = 'Submitted'
  const creating = h.state.handleCreateKey()
  await h.state.handleCreateKey()
  assert.equal(creates.length, 1, 'a pending create must not be duplicated')
  h.state.openCreateModal(); h.state.newKeyName.value = 'New draft'
  creates[0].resolve({ name: 'Submitted' })
  for (let i = 0; i < 8; i++) await Promise.resolve()
  pending[2].resolve([{ key: 'fixture-created', name: 'Submitted' }]); await creating
  assert.equal(h.state.createModalOpen.value, true, 'old creation must not close a newly opened dialog')
  assert.equal(h.state.newKeyName.value, 'New draft')
  h.state.openEditModal({ key: 'fixture-first', name: 'First' })
  h.state.editKeyNameInput.value = 'Submitted rename'
  const editing = h.state.handleSaveEditName()
  await h.state.handleSaveEditName()
  assert.equal(edits.length, 1)
  h.state.openEditModal({ key: 'fixture-other', name: 'Other' })
  edits[0].resolve({})
  for (let i = 0; i < 8; i++) await Promise.resolve()
  pending[3].resolve([]); await editing
  assert.equal(h.state.editModalOpen.value, true)
  assert.equal(h.state.editingKey.value.key, 'fixture-other')
  const leaving = h.state.loadApiKeys(); h.stop()
  pending[4].resolve([{ key: 'fixture-unmounted' }]); await leaving
  assert.equal(h.state.apiKeys.value.length, 0)
  assert.equal(pending[4].signal.aborted, true)
}
checks.push('key lists reject stale and unmounted results; creation and renaming retain newer dialogs')

{
  const creates = [], renames = []
  let listCount = 0
  const h = setup('components/settings/ApiKeyManagerCard.vue', {
    listApiKeys: async () => { listCount++; return [] },
    createApiKey: () => { const request = deferred(); creates.push(request); return request.promise },
    updateApiKeyName: () => { const request = deferred(); renames.push(request); return request.promise },
  })
  h.state.openCreateModal(); h.state.newKeyName.value = 'Retained draft'
  const failedCreate = h.state.handleCreateKey()
  creates[0].reject(new Error('Synthetic creation failure')); await failedCreate
  assert.equal(h.state.creating.value, false)
  assert.equal(h.state.createModalOpen.value, true)
  assert.equal(h.state.newKeyName.value, 'Retained draft')
  const retry = h.state.handleCreateKey()
  creates[1].resolve({ name: 'Retained draft' }); await retry
  assert.equal(h.state.createModalOpen.value, false)
  h.state.openEditModal({ key: 'fixture-key', name: 'Original' })
  h.state.editKeyNameInput.value = 'Rename draft'
  const failedRename = h.state.handleSaveEditName()
  renames[0].reject(new Error('Synthetic rename failure')); await failedRename
  assert.equal(h.state.savingName.value, false)
  assert.equal(h.state.editModalOpen.value, true)
  const renameRetry = h.state.handleSaveEditName()
  h.stop(); renames[1].resolve({}); await renameRetry
  assert.equal(listCount, 1, 'a successful mutation after unmount must not start another list request')
  assert.equal(h.state.editModalOpen.value, true, 'unmounted completion must not change dialog state')
}
checks.push('key mutation failures preserve drafts and permit retry; unmounted mutations cannot refresh or close dialogs')

for (const mode of ['visual', 'yaml', 'explicit logging', 'invalid yaml']) {
  const submissions = []
  const config = level => YAML.stringify({ logging: { level, dump_requests: false }, model_defaults: { drop_unsupported_params: false } })
  const props = vue.reactive({ initialYaml: config('INFO') })
  const h = setup('components/config/VisualConfigEditor.vue', { updateConfigYaml: async content => { submissions.push(content) } }, props)
  h.state.updateGlobalDropUnsupported(true)
  if (mode !== 'visual') h.state.switchTab('yaml')
  if (mode === 'explicit logging') h.state.rawYaml.value = h.state.rawYaml.value.replace('INFO', 'DEBUG')
  const validModelDraft = h.state.rawYaml.value
  if (mode === 'invalid yaml') h.state.rawYaml.value = validModelDraft + '\nnew_model_rule: ['
  props.initialYaml = config('ERROR')
  await vue.nextTick()
  if (mode === 'invalid yaml') {
    assert.equal(h.state.rawYaml.value, validModelDraft + '\nnew_model_rule: [', 'refresh must not replace invalid user YAML')
    h.state.rawYaml.value = validModelDraft
    await h.state.handleSave()
    assert.equal(YAML.parse(submissions[0]).logging.level, 'ERROR',
      'repairing an unrelated YAML syntax error must still preserve the latest logging refresh')
  } else {
    await h.state.handleSave()
    const submitted = YAML.parse(submissions[0])
    assert.equal(submitted.model_defaults.drop_unsupported_params, true)
    assert.equal(submitted.logging.level, mode === 'explicit logging' ? 'DEBUG' : 'ERROR',
      'saving a model draft must not revert logging saved by the other settings card')
  }
  h.stop()
}
for (const scenario of ['latest refresh', 'explicit logging repair', 'repair to visual']) {
  const submissions = []
  const config = level => YAML.stringify({ logging: { level, dump_requests: false }, model_defaults: { drop_unsupported_params: false } })
  const props = vue.reactive({ initialYaml: config('INFO') })
  const h = setup('components/config/VisualConfigEditor.vue', { updateConfigYaml: async content => { submissions.push(content) } }, props)
  h.state.updateGlobalDropUnsupported(true)
  h.state.switchTab('yaml')
  const validDraft = h.state.rawYaml.value
  h.state.rawYaml.value += '\nnew_model_rule: ['
  props.initialYaml = config('ERROR')
  await vue.nextTick()
  props.initialYaml = config('WARNING')
  await vue.nextTick()
  assert.equal(h.state.rawYaml.value, validDraft + '\nnew_model_rule: [')
  h.state.rawYaml.value = scenario === 'explicit logging repair' ? validDraft.replace('INFO', 'DEBUG') : validDraft
  if (scenario === 'repair to visual') h.state.switchTab('visual')
  await h.state.handleSave()
  const submitted = YAML.parse(submissions[0])
  assert.equal(submitted.logging.level, scenario === 'explicit logging repair' ? 'DEBUG' : 'WARNING')
  assert.equal(submitted.model_defaults.drop_unsupported_params, true)
  h.stop()
}
checks.push('model drafts retain the latest logging refresh through YAML syntax repair while explicit logging edits survive')

console.log(JSON.stringify({ passed: true, checks, groups: checks.length, syntheticOnly: true }))
