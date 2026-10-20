import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const buildRoot = path.dirname(fileURLToPath(import.meta.url))
const appRoot = path.resolve(buildRoot, '../../web')
const require = createRequire(path.join(appRoot, 'package.json'))
const vue = require('vue')
const YAML = require('yaml')
const ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const submissions = [], errors = [], activations = [], cooldownResets = []
const toast = { error: message => errors.push(message), success() {}, info() {} }
const systemApi = { updateConfigYaml: async content => { submissions.push(content) } }
const scope = vue.effectScope()

function componentSetup(relative, props, emit = () => {}) {
  const source = fs.readFileSync(path.join(appRoot, 'src', relative), 'utf8')
  const { descriptor } = parse(source)
  const compiled = compileScript(descriptor, { id: 'key-persistence-regression' }).content
  const javascript = ts.transpileModule(compiled, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
  }).outputText
  const module = { exports: {} }
  const context = {
    exports: module.exports, module, console, confirm: () => true,
    require(name) {
      if (name === 'vue') return { ...vue, onMounted() {} }
      if (name === 'vue-router') return { onBeforeRouteLeave() {} }
      if (name === '@/api/system.ts') return { systemApi }
      if (name === '@/stores/toast.ts') return { useToastStore: () => toast }
      if (name === '@/stores/accounts.ts') return {
        useAccountsStore: () => ({ activateAccount: async id => { activations.push(id) } }),
      }
      if (name === '@/stores/system.ts') return {
        useSystemStore: () => ({ clearCooldown: async req => { cooldownResets.push(req) } }),
      }
      if (name === './useCookieGroups.ts') return { useCookieGroups: () => ({ cookieGroups: vue.ref([]) }) }
      if (name === './schema.ts') return loadTypescript('components/config/schema.ts')
      if (name.endsWith('.vue')) return {}
      return require(name)
    },
  }
  vm.runInNewContext(javascript, context)
  return scope.run(() => module.exports.default.setup(props, { expose() {}, emit }))
}

function loadTypescript(relative, globals = {}, imports = {}) {
  const source = fs.readFileSync(path.join(appRoot, 'src', relative), 'utf8')
  const javascript = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(javascript, {
    module, exports: module.exports, ...globals,
    require(name) { return imports[name] ?? require(name) },
  })
  return module.exports
}

function modelCardProps(editor) {
  const { descriptor } = parse(fs.readFileSync(path.join(appRoot, 'src/components/config/VisualConfigEditor.vue'), 'utf8'))
  const binding = descriptor.template.content.match(/<ModelOverrideCard\s[\s\S]*?:models="([^"]+)"/)[1]
  const bindings = Object.fromEntries(Object.entries(editor).map(([key, value]) => [key, vue.unref(value)]))
  return { models: vm.runInNewContext(binding, bindings), filterKeyword: editor.filterKeyword.value }
}

function authHarness() {
  const storage = new Map(), requests = []
  let unauthorized = 0
  const client = loadTypescript('api/client.ts', {
    Headers, FormData,
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
    fetch(url, options) {
      return new Promise(resolve => { requests.push({ url, options, resolve }) })
    },
  })
  client.setUnauthorizedHandler(() => { unauthorized++ })
  const rejectUnauthorized = request => request.resolve({
    ok: false, status: 401, json: async () => ({ detail: 'Synthetic unauthorized response' }),
  })
  return { client, requests, rejectUnauthorized, unauthorized: () => unauthorized }
}

const oldKey = 'fixture-old-key-not-a-real-credential'
const currentKey = 'fixture-current-key-not-a-real-credential'
const config = (key, drop = false) => YAML.stringify({
  api_keys: [{ name: 'fixture', key }],
  logging: { level: 'INFO', dump_requests: false },
  model_defaults: { drop_unsupported_params: drop, profiles: [], models: {} },
})
const props = vue.reactive({ initialYaml: config(oldKey) })
const editor = componentSetup('components/config/VisualConfigEditor.vue', props)

assert.equal(editor.hasUnsavedChanges.value, false, 'initial load must not be dirty')
assert.equal(editor.parsedConfig.value.api_keys, undefined, 'editor must never retain API keys')
assert(!editor.rawYaml.value.includes(oldKey), 'YAML text must not expose cached keys')

editor.updateGlobalDropUnsupported(true)
assert.equal(editor.hasUnsavedChanges.value, true)
props.initialYaml = config(currentKey)
await vue.nextTick()
assert.equal(editor.parsedConfig.value.model_defaults.drop_unsupported_params, true, 'refresh must preserve model draft')
await editor.handleSave()
assert.equal(submissions.length, 1)
assert.equal(YAML.parse(submissions[0]).api_keys, undefined, 'visual save must not submit stale keys')
assert.equal(YAML.parse(submissions[0]).model_defaults.drop_unsupported_params, true)
assert.equal(editor.hasUnsavedChanges.value, false)

props.initialYaml = config(currentKey, true)
await vue.nextTick()
assert.equal(editor.hasUnsavedChanges.value, false, 'key-only refresh must not create unsaved changes')

editor.switchTab('yaml')
editor.rawYaml.value = config(oldKey, false)
await editor.handleSave()
assert.equal(submissions.length, 2)
assert.equal(YAML.parse(submissions[1]).api_keys, undefined, 'pasted YAML must not write API keys')
assert.equal(YAML.parse(submissions[1]).model_defaults.drop_unsupported_params, false)
assert(!editor.rawYaml.value.includes(oldKey))

editor.rawYaml.value = config(oldKey, true)
props.initialYaml = config(currentKey, false)
await vue.nextTick()
assert.equal(YAML.parse(editor.rawYaml.value).model_defaults.drop_unsupported_params, true, 'refresh must preserve YAML model draft')

editor.rawYaml.value = 'model_defaults: [broken'
await editor.handleSave()
assert.equal(submissions.length, 2, 'invalid YAML must not be sent')
assert.equal(editor.hasUnsavedChanges.value, true)
assert.equal(errors.length, 1)

const overrideEditor = componentSetup('components/config/VisualConfigEditor.vue', vue.reactive({ initialYaml: YAML.stringify({
  model_defaults: { models: {
    'gemini-visible': { default_tools: ['google_search'] },
    'hidden-existing': { generation_config_defaults: { temperature: 0.75 } },
  } },
}) }))
overrideEditor.filterKeyword.value = 'GeMiNi'
const cardProps = vue.reactive(modelCardProps(overrideEditor))
const card = componentSetup('components/config/ModelOverrideCard.vue', cardProps, (event, models) => {
  assert.equal(event, 'update:models')
  overrideEditor.updateModelsOverride(models)
  Object.assign(cardProps, modelCardProps(overrideEditor))
})
card.updateModelField('gemini-visible', 'disable_safety_settings', true)
assert.equal(overrideEditor.parsedConfig.value.model_defaults.models['hidden-existing'].generation_config_defaults.temperature, 0.75,
  'editing a filtered model must retain hidden rules')
assert.deepEqual(Object.keys(card.visibleModels.value), ['gemini-visible'], 'filter must affect display only')
card.newModelName.value = 'hidden-existing'
card.handleAddModel()
assert.equal(cardProps.models['hidden-existing'].generation_config_defaults.temperature, 0.75, 'adding an existing hidden name must not reset its rule')
card.newModelName.value = 'models/new-model'
card.handleAddModel()
assert(cardProps.models['new-model'], 'adding while filtered must retain the new rule')
card.handleDeleteModel('gemini-visible')
assert.equal(cardProps.models['gemini-visible'], undefined)
assert(cardProps.models['new-model'])
assert(cardProps.models['hidden-existing'])
assert.deepEqual(Object.keys(card.visibleModels.value), [])
await overrideEditor.handleSave()
const savedOverrides = YAML.parse(submissions.at(-1)).model_defaults.models
assert.deepEqual(Object.keys(savedOverrides).sort(), ['hidden-existing', 'new-model'])
assert.equal(savedOverrides['hidden-existing'].generation_config_defaults.temperature, 0.75)

for (const scenario of ['old token', 'anonymous request', 'explicit candidate']) {
  const h = authHarness()
  if (scenario !== 'anonymous request') h.client.setToken('fixture-old-token')
  const options = scenario === 'explicit candidate' ? { headers: { Authorization: 'Bearer fixture-bad-candidate' } } : {}
  const request = h.client.request('/stats', options)
  if (scenario !== 'explicit candidate') h.client.setToken('fixture-current-token')
  h.rejectUnauthorized(h.requests[0])
  await assert.rejects(request, error => error.status === 401)
  assert.equal(h.client.getToken(), scenario === 'explicit candidate' ? 'fixture-old-token' : 'fixture-current-token', scenario)
  assert.equal(h.unauthorized(), 0, `${scenario} must not log out the current session`)
}
for (const token of ['', 'fixture-current-token']) {
  const h = authHarness()
  h.client.setToken(token)
  const request = h.client.request('/stats')
  h.rejectUnauthorized(h.requests[0])
  await assert.rejects(request, error => error.status === 401)
  assert.equal(h.client.getToken(), '')
  assert.equal(h.unauthorized(), 1, 'a 401 for the current session must still trigger authentication handling')
}

for (const scenario of ['replacement token', 'current 401', 'server 500', 'network failure']) {
  const h = authHarness()
  h.client.setToken('fixture-old-token')
  let rejectVerification
  const auth = loadTypescript('stores/auth.ts', {}, {
    pinia: { defineStore: (_name, setup) => setup },
    vue,
    '@/api/client.ts': h.client,
    '@/api/auth.ts': { authApi: {
      checkAuth: async () => ({ auth_enabled: true }),
      verifyToken: () => new Promise((_resolve, reject) => { rejectVerification = reject }),
    } },
  }).useAuthStore()
  const checking = auth.checkAuth()
  for (let i = 0; i < 8; i++) await Promise.resolve()
  assert.equal(typeof rejectVerification, 'function')
  if (scenario === 'replacement token') auth.updateToken('fixture-current-token')
  const error = scenario === 'network failure'
    ? new Error('Synthetic offline failure')
    : new h.client.ApiError(scenario === 'server 500' ? 500 : 401, 'Synthetic verification failure')
  rejectVerification(error)
  await checking
  const expectedToken = scenario === 'replacement token' ? 'fixture-current-token' : scenario === 'current 401' ? '' : 'fixture-old-token'
  assert.equal(auth.token.value, expectedToken, scenario)
  assert.equal(h.client.getToken(), expectedToken, scenario)
}

{
  const loggingRequests = []
  systemApi.updateLoggingConfig = submitted => new Promise(resolve => { loggingRequests.push({ submitted, resolve }) })
  const initialConfig = { log_level: 'INFO', dump_requests: false, dump_to_file: false, dump_dir: 'dumps', debug_env_active: false }
  const loggingProps = vue.reactive({ initialConfig })
  const logging = componentSetup('components/settings/LogConfigCard.vue', loggingProps)
  loggingProps.initialConfig = { ...initialConfig, log_level: 'ERROR' }
  await vue.nextTick()
  assert.equal(logging.logLevel.value, 'ERROR', 'clean logging settings must follow a server refresh')
  logging.logLevel.value = 'WARNING'
  loggingProps.initialConfig = { ...loggingProps.initialConfig, debug_env_active: true, port: 8080 }
  await vue.nextTick()
  assert.equal(logging.logLevel.value, 'WARNING', 'saving another config card must not erase the logging draft')
  assert.equal(logging.debugEnvActive.value, true, 'runtime environment status must still refresh')

  const save = logging.handleSave()
  await logging.handleSave()
  assert.equal(loggingRequests.length, 1, 'a pending logging save must not be duplicated')
  assert.equal(loggingRequests[0].submitted.level, 'WARNING')
  logging.logLevel.value = 'DEBUG'
  logging.dumpDir.value = 'fixture-next-dumps'
  const firstResponse = { ok: true, ...initialConfig, log_level: 'WARNING', debug_env_active: true }
  loggingRequests[0].resolve(firstResponse)
  await save
  assert.equal(logging.logLevel.value, 'DEBUG', 'a save response must not overwrite edits made during that save')
  assert.equal(logging.dumpDir.value, 'fixture-next-dumps')
  loggingProps.initialConfig = firstResponse
  await vue.nextTick()
  assert.equal(logging.logLevel.value, 'DEBUG')
  assert.equal(logging.dumpDir.value, 'fixture-next-dumps', 'post-save config reload must retain newer edits')

  const secondSave = logging.handleSave()
  assert.equal(loggingRequests[1].submitted.level, 'DEBUG')
  assert.equal(loggingRequests[1].submitted.dump_dir, 'fixture-next-dumps')
  loggingRequests[1].resolve({ ...firstResponse, log_level: 'DEBUG', dump_dir: 'fixture-normalized-dumps' })
  await secondSave
  assert.equal(logging.dumpDir.value, 'fixture-normalized-dumps', 'an unchanged submitted draft must accept the saved server values')
}

const table = componentSetup('components/accounts/AccountTable.vue', vue.reactive({ accounts: [], activeId: '' }))
await table.handleReverifyAccount({ id: 'fixture-expired-account' })
assert.deepEqual(activations, ['fixture-expired-account'])
assert.equal(cooldownResets.length, 0, 'reverify must not erase counters or quota cooldowns before activation')
scope.stop()

const result = {
  passed: true,
  checks: [
    'initial editor load has no unsaved marker',
    'API keys omitted from editor state and YAML',
    'unsaved visual and YAML model drafts survive config refresh',
    'visual save and pasted legacy YAML never submit API keys',
    'API-key-only refresh does not mark editor dirty',
    'invalid YAML cannot be submitted',
    'filtered model editing, adding and deletion preserve hidden rules in saved YAML',
    'late and candidate-token 401 responses preserve the current authenticated session',
    'current-session and unauthenticated 401 responses still trigger authentication handling',
    'late auth-store verification cannot clear a replacement token',
    'network and server failures preserve the token while its actual 401 clears it',
    'logging refresh and save completion preserve unsaved and newly edited drafts',
    'expired-session reverify does not clear request counters or quota cooldowns',
  ],
}
console.log(JSON.stringify(result))
