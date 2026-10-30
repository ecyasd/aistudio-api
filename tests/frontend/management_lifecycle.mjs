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
const checks = []
const deferred = () => {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function execute(relative, overrides = {}, globals = {}, component = false) {
  let source = fs.readFileSync(path.join(webRoot, 'src', relative), 'utf8')
  if (component) source = compileScript(parse(source).descriptor, { id: 'management-lifecycle' }).content
  const javascript = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(javascript, {
    module, exports: module.exports, console, ...globals,
    require(name) {
      if (name in overrides) return overrides[name]
      if (name.endsWith('.vue')) return {}
      return require(name)
    },
  }, { filename: relative })
  return module.exports
}

function component(relative, props, accountsStore, globals = {}) {
  const unmounts = [], events = []
  const scope = vue.effectScope()
  const exports = execute(relative, {
    vue: { ...vue, onUnmounted: callback => unmounts.push(callback) },
    '@/stores/accounts.ts': { useAccountsStore: () => accountsStore },
  }, globals, true)
  const state = scope.run(() => exports.default.setup(props, { expose() {}, emit: (...event) => events.push(event) }))
  return { state, events, stop() { unmounts.forEach(callback => callback()); scope.stop() } }
}

function authHarness() {
  let saved = ''
  const pending = []
  const client = {
    ApiError: class extends Error {},
    getToken: () => saved,
    setToken: value => { saved = value },
    clearToken: () => { saved = '' },
  }
  const store = execute('stores/auth.ts', {
    pinia: { defineStore: (_name, setup) => setup }, vue,
    '@/api/client.ts': client,
    '@/api/auth.ts': { authApi: {
      checkAuth: async () => ({ auth_enabled: true }),
      verifyToken: () => { const request = deferred(); pending.push(request); return request.promise },
    } },
  }).useAuthStore()
  return { store, client, pending }
}

for (const action of ['logout', 'replacement', 'external replacement', 'newer login']) {
  const h = authHarness()
  const oldLogin = h.store.login('fixture-old-token')
  let newerLogin
  if (action === 'logout') h.store.logout()
  if (action === 'replacement') h.store.updateToken('fixture-current-token')
  if (action === 'external replacement') h.client.setToken('fixture-current-token')
  if (action === 'newer login') {
    newerLogin = h.store.login('fixture-current-token')
    h.pending[1].resolve({})
    assert.equal(await newerLogin, true)
  }
  h.pending[0].resolve({})
  assert.equal(await oldLogin, false, `stale login after ${action} must not authenticate`)
  assert.equal(h.client.getToken(), action === 'logout' ? '' : 'fixture-current-token')
}
checks.push('late successful login cannot undo logout or replace a newer credential or login')

{
  const h = authHarness()
  h.store.updateToken('fixture-token')
  const checking = h.store.checkAuth()
  for (let i = 0; i < 8; i++) await Promise.resolve()
  h.store.logout()
  h.pending[0].resolve({})
  assert.equal(await checking, false, 'a successful verification after logout must not report authenticated')
  const retry = h.store.login('fixture-retry-token')
  h.pending[1].resolve({})
  assert.equal(await retry, true)
  assert.equal(h.store.token.value, 'fixture-retry-token')
}
checks.push('verification respects logout while a subsequent explicit login still succeeds')

function readerHarness() {
  const readers = [], imports = []
  class FileReaderFixture {
    readyState = 0
    aborts = 0
    readAsText(file) { this.file = file; this.readyState = 1; readers.push(this) }
    abort() { this.aborts++; this.readyState = 2 }
    finish(text) { this.readyState = 2; this.onload?.({ target: { result: text } }) }
    fail() { this.readyState = 2; this.onerror?.() }
  }
  const props = vue.reactive({ modelValue: true })
  const modal = component('components/modals/CookieImportModal.vue', props, {
    importing: false,
    importBundle: async payload => { imports.push(payload); return true },
  }, { FileReader: FileReaderFixture })
  modal.state.activeTab.value = 'file'
  const select = name => modal.state.onFileChange({ target: { files: [{ name, size: 100 }] } })
  return { ...modal, props, readers, imports, select }
}

{
  const h = readerHarness()
  h.select('old-fixture.json')
  h.readers[0].finish('{"accounts":[{"id":"old-fixture"}]}')
  h.select('current-fixture.json')
  assert.equal(h.state.selectedFileContent.value, '', 'new selection must immediately discard the previous bundle')
  await h.state.handleSubmit()
  assert.equal(h.imports.length, 0, 'a selected file must finish reading before it can be imported')
  h.readers[1].finish('{"accounts":[{"id":"current-fixture"}]}')
  await h.state.handleSubmit()
  assert.equal(h.imports[0].content, '{"accounts":[{"id":"current-fixture"}]}')
  assert.deepEqual(Object.keys(h.imports[0]), ['content'], 'file import submits browser-read JSON only')
  h.stop()
}
checks.push('selecting a new bundle never imports the previous file while its replacement is loading')

for (const action of ['replacement', 'clear', 'close', 'unmount']) {
  const h = readerHarness()
  h.select('old-fixture.json')
  if (action === 'replacement') {
    h.select('current-fixture.json')
    h.readers[1].finish('{"accounts":[{"id":"current-fixture"}]}')
  }
  if (action === 'clear') h.state.clearSelectedFile()
  if (action === 'close') { h.props.modelValue = false; await vue.nextTick() }
  if (action === 'unmount') h.stop()
  h.readers[0].finish('{"accounts":[{"id":"stale-fixture"}]}')
  assert.equal(h.readers[0].aborts, 1, `${action} must abort the pending reader`)
  assert.equal(h.state.selectedFileContent.value,
    action === 'replacement' ? '{"accounts":[{"id":"current-fixture"}]}' : '',
    `${action} must ignore a late file reader callback`)
  if (action !== 'unmount') h.stop()
}
checks.push('replacement, clear, close and unmount abort file reads and reject late results')

{
  const h = readerHarness()
  h.select('broken-fixture.json')
  h.readers[0].fail()
  assert.equal(h.state.selectedFileContent.value, '')
  assert.equal(h.state.readingFile.value, false)
  h.select('retry-fixture.json')
  h.readers[1].finish('{"accounts":[]}')
  await h.state.handleSubmit()
  assert.equal(h.imports.length, 1, 'a failed file read must permit a later successful retry')
  h.stop()
}
checks.push('file read errors clear pending state and allow retry')

for (const action of ['replacement file', 'changed text', 'switched tab', 'close and reopen', 'unmount']) {
  const requests = []
  const props = vue.reactive({ modelValue: true })
  const h = component('components/modals/CookieImportModal.vue', props, {
    importing: false,
    importBundle: () => { const request = deferred(); requests.push(request); return request.promise },
    importCookies: () => { const request = deferred(); requests.push(request); return request.promise },
  })
  h.state.autoProbe.value = false
  h.state.cookieInput.value = 'fixture-original-cookie'
  h.state.activeTab.value = action === 'replacement file' ? 'file' : 'text'
  h.state.selectedFileContent.value = '{"accounts":[{"id":"original-fixture"}]}'
  const submit = h.state.handleSubmit()
  if (action === 'replacement file') h.state.selectedFileContent.value = '{"accounts":[{"id":"current-fixture"}]}'
  if (action === 'changed text') h.state.cookieInput.value = 'fixture-current-cookie'
  if (action === 'switched tab') h.state.activeTab.value = 'file'
  if (action === 'close and reopen') { props.modelValue = false; props.modelValue = true }
  if (action === 'unmount') h.stop()
  requests[0].resolve(true)
  await submit
  assert.equal(h.events.length, 0, `${action} must not be closed by a previous import`)
  if (action === 'replacement file') assert.equal(h.state.selectedFileContent.value, '{"accounts":[{"id":"current-fixture"}]}')
  if (action === 'changed text') assert.equal(h.state.cookieInput.value, 'fixture-current-cookie')
  if (action !== 'unmount') h.stop()
}
checks.push('late import completions preserve newer file and text drafts and ignore closed or unmounted dialogs')

{
  const errors = []
  let failUpdate = true
  const store = execute('stores/accounts.ts', {
    pinia: { defineStore: (_name, setup) => setup }, vue,
    '@/api/accounts.ts': { accountsApi: {
      update: async () => { if (failUpdate) throw new Error('Synthetic rename failure') },
      list: async () => [], getActive: async () => null,
    } },
    './system.ts': { useSystemStore: () => ({ fetchRotation: async () => {}, rotation: null }) },
    './toast.ts': { useToastStore: () => ({ success() {}, error: message => errors.push(message) }) },
  }).useAccountsStore()
  assert.equal(await store.updateAccountName('fixture-id', 'Draft name'), false)
  assert.equal(errors.length, 1)
  failUpdate = false
  assert.equal(await store.updateAccountName('fixture-id', 'Draft name'), true)
}
checks.push('the account store distinguishes failed and successful rename requests for its dialog')

{
  const requests = []
  const props = vue.reactive({ modelValue: true, account: { id: 'fixture-a', name: 'Old name' } })
  const h = component('components/modals/EditAccountModal.vue', props, {
    updateAccountName: (...args) => { const request = deferred(); requests.push({ args, ...request }); return request.promise },
  })
  h.state.inputName.value = 'Retryable draft'
  const failedSave = h.state.handleSave()
  requests[0].resolve(false)
  await failedSave
  assert.equal(h.events.length, 0, 'failed renaming must leave the dialog open')
  assert.equal(h.state.inputName.value, 'Retryable draft')

  const saving = h.state.handleSave()
  await h.state.handleSave()
  assert.equal(requests.length, 2, 'a pending rename must not be duplicated')
  props.account = { id: 'fixture-b', name: 'New account' }
  await vue.nextTick()
  h.state.inputName.value = 'New account draft'
  requests[1].resolve(true)
  await saving
  assert.equal(h.events.length, 0, 'an old save must not close a newly selected account dialog')
  assert.equal(h.state.inputName.value, 'New account draft')
  const nextSave = h.state.handleSave()
  requests[2].resolve(true)
  await nextSave
  assert.deepEqual(h.events, [['update:modelValue', false]], 'a current successful rename closes its dialog')
  h.stop()
}
checks.push('rename failures preserve the draft and old save completion cannot close another account')

for (const action of ['changed draft', 'close and reopen', 'unmount']) {
  const request = deferred()
  const props = vue.reactive({ modelValue: true, account: { id: 'fixture-a', name: 'Old name' } })
  const h = component('components/modals/EditAccountModal.vue', props, { updateAccountName: () => request.promise })
  h.state.inputName.value = 'Submitted name'
  props.account = { id: 'fixture-a', name: 'Server refresh' }
  assert.equal(h.state.inputName.value, 'Submitted name', 'same-account refresh must not reset an open draft')
  const submit = h.state.handleSave()
  if (action === 'changed draft') h.state.inputName.value = 'Newer name'
  if (action === 'close and reopen') { props.modelValue = false; props.modelValue = true }
  if (action === 'unmount') h.stop()
  request.resolve(true)
  await submit
  assert.equal(h.events.length, 0, `${action} must not be closed by a previous rename`)
  if (action === 'changed draft') assert.equal(h.state.inputName.value, 'Newer name')
  if (action !== 'unmount') h.stop()
}
checks.push('late rename completions preserve newer drafts and ignore reopened or unmounted dialogs')

console.log(JSON.stringify({ passed: true, checks }))
