import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../web')
const require = createRequire(path.join(root, 'package.json'))
const vue = require('vue')
const ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const { renderToString } = require('@vue/server-renderer')
const checks = []

function loader({ inline = false, hooks = {}, globals = {}, imports = {}, stubComponents = false } = {}) {
  const cache = new Map()
  function load(relative) {
    const filename = path.resolve(root, 'src', relative)
    if (cache.has(filename)) return cache.get(filename)
    let source = fs.readFileSync(filename, 'utf8')
    if (filename.endsWith('.vue')) {
      source = compileScript(parse(source).descriptor, { id: relative, inlineTemplate: inline }).content
    }
    const code = ts.transpileModule(source, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
    }).outputText
    const module = { exports: {} }
    cache.set(filename, module.exports)
    vm.runInNewContext(code, {
      module, exports: module.exports, console, confirm: () => true, ...globals,
      require(name) {
        if (name in imports) return imports[name]
        if (name === 'vue') return { ...vue, ...hooks }
        if (stubComponents && name.endsWith('.vue')) return {}
        if (name.startsWith('.')) return load(path.relative(path.join(root, 'src'), path.resolve(path.dirname(filename), name)))
        if (name.startsWith('@/')) return load(name.slice(2))
        return require(name)
      },
    }, { filename })
    cache.set(filename, module.exports)
    return module.exports
  }
  return load
}

{
  const load = loader({ inline: true })
  const profile = { name: 'synthetic', default_tools: ['google_search', 'code_execution'] }
  const app = vue.createSSRApp(load('components/config/ProfileCard.vue').default, { profile, index: 0 })
  const html = await renderToString(app)
  assert(html.includes('google_search') && html.includes('code_execution'))
  const plain = loader()
  const events = []
  const state = plain('components/config/ConfigTagList.vue').default.setup(
    vue.reactive({ modelValue: profile.default_tools }),
    { expose() {}, emit: (...args) => events.push(args) },
  )
  state.addTag('image_search')
  assert.deepEqual(Array.from(events.at(-1)[1]), ['google_search', 'code_execution', 'image_search'])
  state.removeTag(0)
  assert.deepEqual(Array.from(events.at(-1)[1]), ['code_execution'])
  checks.push('profile default tools render existing values and edits retain the full list')
}

{
  class Element {
    isConnected = true
    items = []
    focus() { document.activeElement = this }
    querySelectorAll() { return this.items }
    getClientRects() { return [1] }
    contains(item) { return item === this || this.items.includes(item) }
  }
  const document = { body: { style: { overflow: 'auto' } }, activeElement: new Element() }
  const initialFocus = document.activeElement
  const listeners = new Set()
  const unmounts = []
  const load = loader({
    globals: { document, HTMLElement: Element, window: {
      addEventListener(_name, callback) { listeners.add(callback) },
      removeEventListener(_name, callback) { listeners.delete(callback) },
    } },
    hooks: { onMounted(callback) { callback() }, onUnmounted(callback) { unmounts.push(callback) } },
  })
  const component = load('components/ui/Modal.vue').default
  function mount(open = true) {
    const scope = vue.effectScope(), props = vue.reactive({ modelValue: open }), events = []
    const state = scope.run(() => component.setup(props, { expose() {}, emit: (...args) => events.push(args) }))
    state.panel.value = new Element()
    const callback = unmounts.at(-1)
    return { props, state, events, stop() { callback(); scope.stop(); state.panel.value.isConnected = false } }
  }
  const first = mount()
  await vue.nextTick()
  assert.equal(document.body.style.overflow, 'hidden')
  assert.equal(document.activeElement, first.state.panel.value)
  const second = mount()
  await vue.nextTick()
  first.state.handleKeydown({ key: 'Escape', preventDefault() {} })
  assert.equal(first.events.length, 0, 'Escape must affect only the top dialog')
  second.state.handleKeydown({ key: 'Escape', preventDefault() {} })
  assert.equal(second.events[0][0], 'update:modelValue')
  second.props.modelValue = false
  await vue.nextTick()
  assert.equal(document.body.style.overflow, 'hidden', 'closing one dialog cannot unlock another')
  assert.equal(document.activeElement, first.state.panel.value)
  second.stop()
  first.stop()
  assert.equal(document.body.style.overflow, 'auto', 'unmount restores the original overflow')
  assert.equal(document.activeElement, initialFocus)
  assert.equal(listeners.size, 0)
  const closed = mount(false)
  closed.stop()
  assert.equal(document.body.style.overflow, 'auto')
  checks.push('dialog unmount and overlap restore scrolling, focus and owned event listeners')
}

for (const filter of ['expired', 'search', 'all']) {
  const confirmations = [], deleted = []
  let allow = false
  const load = loader({
    stubComponents: true,
    hooks: { onMounted() {} },
    globals: { confirm(message) { confirmations.push(message); return allow } },
    imports: {
      '@/stores/accounts.ts': { useAccountsStore: () => ({ deleteCookieGroup: async id => { deleted.push(id) } }) },
      '@/stores/system.ts': { useSystemStore: () => ({}) },
      '@/stores/toast.ts': { useToastStore: () => ({}) },
      '@/api/system.ts': { systemApi: {} },
      '@/composables/usePolling.ts': { usePolling() {} },
    },
  })
  const scope = vue.effectScope()
  const accounts = [
    { id: 'healthy', name: 'healthy', cookie_id: 'cookie_fixture', session_expired: false },
    { id: 'expired', name: 'expired', cookie_id: 'cookie_fixture', session_expired: true },
  ]
  const props = vue.reactive({ accounts, activeId: 'healthy' })
  const state = scope.run(() => load('components/accounts/AccountTable.vue').default.setup(props, { expose() {}, emit() {} }))
  if (filter === 'expired') state.statusFilter.value = 'expired'
  if (filter === 'search') state.searchKeyword.value = 'expired'
  const group = state.cookieGroups.value[0]
  await state.handleDeleteCookieGroup(group)
  assert.equal(deleted.length, 0, 'rejecting confirmation cannot delete a group')
  assert(confirmations[0].includes('全部 2 个子账号'))
  if (filter !== 'all') assert(confirmations[0].includes('另外 1 个也会被删除'))
  allow = true
  await state.handleDeleteCookieGroup(group)
  assert.deepEqual(deleted, ['cookie_fixture'])
  scope.stop()
}
checks.push('whole-group deletion confirms all members including accounts hidden by search or status filters')

console.log(JSON.stringify({ passed: true, checks, syntheticOnly: true }))
