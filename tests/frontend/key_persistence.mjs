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

function componentSetup(relative, props) {
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
      if (name.endsWith('.vue')) return {}
      return require(name)
    },
  }
  vm.runInNewContext(javascript, context)
  return scope.run(() => module.exports.default.setup(props, { expose() {}, emit() {} }))
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
    'expired-session reverify does not clear request counters or quota cooldowns',
  ],
}
console.log(JSON.stringify(result))
