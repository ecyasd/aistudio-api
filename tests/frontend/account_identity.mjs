import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'

const scriptRoot = new URL('../../src/aistudio_api/infrastructure/browser/js/', import.meta.url)
const extract = fs.readFileSync(new URL('extract_email.js', scriptRoot), 'utf8').trim()
const check = fs.readFileSync(new URL('check_identity.js', scriptRoot), 'utf8')
const selected = 'selected@example.invalid'
const other = 'other@example.invalid'

// A small synthetic DOM implements only browser APIs used by the scripts. No
// browser, external dependency, stored session or network request is involved.
class Element {
  constructor(tag, attrs = {}, text = '', parent = null, style = {}) {
    this.tag = tag
    this.attrs = attrs
    this.innerText = text
    this.parent = parent
    this.style = { visibility: 'visible', display: 'block', ...style }
  }

  getAttribute(name) { return this.attrs[name] ?? null }

  matches(selector) {
    const parts = selector.trim().match(/(?:[^\s[]|\[[^\]]*\])+/g) || []
    const simple = (el, part) => {
      const tag = part.match(/^[\w-]+/)?.[0]
      if (tag && el.tag !== tag) return false
      for (const [, id] of part.matchAll(/#([\w-]+)/g)) {
        if (el.attrs.id !== id) return false
      }
      for (const [, name] of part.matchAll(/\.([\w-]+)/g)) {
        if (!(el.attrs.class || '').split(/\s+/).includes(name)) return false
      }
      for (const [, name, op, value] of part.matchAll(/\[([\w-]+)(?:(\^=|\*=|=)"([^"]*)")?\]/g)) {
        const actual = el.getAttribute(name)
        if (actual === null) return false
        if (op === '=' && actual !== value) return false
        if (op === '^=' && !actual.startsWith(value)) return false
        if (op === '*=' && !actual.includes(value)) return false
      }
      return true
    }
    let current = this
    if (!parts.length || !simple(current, parts.pop())) return false
    while (parts.length) {
      const part = parts.pop()
      current = current.parent
      while (current && !simple(current, part)) current = current.parent
      if (!current) return false
    }
    return true
  }

  closest(selectors) {
    for (let el = this; el; el = el.parent) {
      if (selectors.split(',').some(selector => el.matches(selector))) return el
    }
    return null
  }

  getClientRects() {
    for (let el = this; el; el = el.parent) {
      if ('hidden' in el.attrs || el.style.display === 'none') return []
    }
    return [{}]
  }
}

function page(elements = [], href = 'https://aistudio.google.com/u/0/prompts/new_chat') {
  const document = {
    body: { innerText: `Unrelated prompt ${selected}` },
    cookie: `fixture_account=${selected}`,
    querySelectorAll: selectors => elements.filter(el => selectors.split(',').some(selector => el.matches(selector))),
  }
  const window = {
    location: { href },
    getComputedStyle: el => el.style,
    WIZ_global_data: { unrelatedAccount: selected },
  }
  const context = vm.createContext({ document, window, URL })
  return {
    extract: () => vm.runInContext(extract, context),
    check: (email = selected, authUser = '0') => vm.runInContext(`(${check})`, context)({ email, authUser }),
  }
}

function switcher(email, parent = new Element('ms-account-switcher'), style = {}) {
  return new Element('button', { 'aria-label': `Account ${email}` }, '', parent, style)
}

let groups = 0
function test(name, run) {
  run()
  groups++
  console.log(`ok - ${name}`)
}

test('active switcher wins over unrelated aria labels, chooser rows and prompt text', () => {
  const elements = [
    new Element('button', { 'aria-label': `Available account ${other}` }),
    new Element('button', { 'aria-label': `Google Account: ${other}` }, '', new Element('div', { role: 'option' })),
    new Element('textarea', { 'aria-label': `Prompt ${other}` }, other),
    new Element('a', { href: 'https://accounts.google.com/SignOutOptions', 'aria-label': other }),
    switcher(selected.toUpperCase()),
  ]
  assert.equal(page(elements).extract(), selected)
  assert.equal(page(elements).check(), true)
  assert.equal(page(elements).check(other), false)
})

test('visible current Google SignOutOptions control is trusted', () => {
  const current = new Element('a', {
    href: 'https://accounts.google.com/SignOutOptions',
    'aria-label': `Google Account: ${selected.toUpperCase()}`,
  })
  assert.equal(page([current]).extract(), selected)
  assert.equal(page([current]).check(), true)
})

test('hidden menus, unselected aria, prompts, body, cookies and globals are not identity evidence', () => {
  const candidates = [
    new Element('button', { 'aria-label': `Available account ${selected}` }),
    new Element('textarea', {}, selected),
    switcher(selected, new Element('ms-account-switcher', { hidden: '' })),
    switcher(selected, new Element('ms-account-switcher', { 'aria-hidden': 'true' })),
    switcher(selected, new Element('ms-account-switcher', { role: 'menu' })),
    switcher(selected, new Element('ms-account-switcher', { role: 'listbox' })),
    switcher(selected, new Element('ms-account-switcher'), { visibility: 'hidden' }),
    switcher(selected, new Element('ms-account-switcher'), { display: 'none' }),
  ]
  assert.equal(page(candidates).extract(), null)
  assert.equal(page(candidates).check(), false)
  assert.equal(page().check(), false)
})

test('a verified different active account is rejected even when expected identity appears elsewhere', () => {
  const current = page([switcher(other)])
  assert.equal(current.extract(), other)
  assert.equal(current.check(), false)
  assert.equal(current.check(other), true)
})

test('conflicting current controls fail closed in both identity tiers', () => {
  const active = page([switcher(selected), switcher(other)])
  assert.equal(active.extract(), null)
  assert.equal(active.check(), false)
  const google = page([selected, other].map(email => new Element('a', {
    href: 'https://accounts.google.com/SignOutOptions', 'aria-label': email,
  })))
  assert.equal(google.extract(), null)
  assert.equal(google.check(), false)
})

test('numeric authuser query and route must agree with the selected slot', () => {
  const valid = [
    ['https://aistudio.google.com/u/2/prompts/new_chat', '2'],
    ['https://aistudio.google.com/prompts/new_chat?authuser=2', '2'],
    ['https://aistudio.google.com/u/002/prompts/new_chat?authuser=02', '2'],
    ['https://aistudio.google.com/u/9007199254740993/prompts/new_chat', '9007199254740993'],
  ]
  for (const [href, slot] of valid) assert.equal(page([switcher(selected)], href).check(selected, slot), true)
  const invalid = [
    'https://aistudio.google.com/u/2/prompts/new_chat?authuser=3',
    'https://aistudio.google.com/prompts/new_chat?authuser=2&authuser=2',
    'https://aistudio.google.com/prompts/new_chat?authuser=selected@example.invalid',
    'https://aistudio.google.com/u/invalid/prompts/new_chat',
    'https://aistudio.google.com/u/2extra/prompts/new_chat',
    'https://aistudio.google.com/u/9007199254740993/prompts/new_chat',
  ]
  for (const href of invalid) assert.equal(page([switcher(selected)], href).check(selected, '2'), false)
  assert.equal(page([switcher(selected)]).check(selected, 'not-numeric'), false)
})

test('non-AI Studio domains and insecure origins are rejected', () => {
  for (const href of [
    'https://accounts.google.com/u/0/',
    'https://aistudio.google.com.example.invalid/u/0/',
    'http://aistudio.google.com/u/0/',
  ]) assert.equal(page([switcher(selected)], href).check(), false)
})

test('identity check embeds the exact standalone extractor to prevent drift', () => {
  assert.equal(check.includes(`${extract};`), true)
})

console.log(`${groups} account identity regression groups passed`)
