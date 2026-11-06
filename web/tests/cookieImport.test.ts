import assert from 'node:assert/strict'
import test from 'node:test'
import { createCookieSourceRegistry, identifyCookieSource, pendingCookieSources, MAX_COOKIE_IMPORT_BYTES } from '../src/utils/cookieImport.ts'

const cookies = [
  { name: 'SID', value: 'fixture_sid', domain: '.google.com', path: '/' },
  { name: 'SAPISID', value: 'fixture_sapisid', domain: '.google.com', path: '/' },
]

test('cookie headers keep values and only discard surrounding whitespace and BOM', () => {
  const input = 'Cookie: SID=fixture_sid; SAPISID=fixture_sapisid=='
  const source = identifyCookieSource('粘贴内容', `\uFEFF  ${input}\n`)
  assert.equal(source.content, input)
  assert.equal(source.kind, 'cookies')
  assert.equal(source.format, 'Cookie 请求头')
})

test('cookie arrays count cookies rather than accounts', () => {
  const input = JSON.stringify(cookies, null, 2)
  const source = identifyCookieSource('cookies.json', input)
  assert.equal(source.kind, 'cookies')
  assert.equal(source.count, 2)
  assert.equal(source.content, input)
})

test('a bare cookie map is sent unchanged for server normalization', () => {
  const input = JSON.stringify({ SID: 'fixture_sid', SAPISID: 'fixture_sapisid' })
  const source = identifyCookieSource('cookies.json', input)
  assert.equal(source.kind, 'cookies')
  assert.equal(source.count, 2)
  assert.equal(source.content, input)
})

test('account bundles retain structured credentials, identifiers and aliases', () => {
  const input = JSON.stringify({
    version: 1,
    profiles: [
      { id: 'fixture-account', label: 'Work', authUser: 1, g1Tier: 'TIER0', cookies },
      { account_id: 'fixture-second', name: 'Personal', storage_state: { cookies, origins: [] } },
    ],
  }, null, 2)
  const source = identifyCookieSource('accounts.json', input)
  assert.equal(source.kind, 'bundle')
  assert.equal(source.count, 2)
  assert.equal(source.content, input)
  assert.equal(source.hasExtraMetadata, true)
})

test('bundles keep invalid items so the server can reject the whole source', () => {
  const input = JSON.stringify({ accounts: [{ cookies }, { name: 'Missing cookies' }] })
  const source = identifyCookieSource('accounts.json', input)
  assert.equal(source.kind, 'bundle')
  assert.equal(source.count, 2)
  assert.equal(source.content, input)
})

test('AIS2 storage state retains account metadata and identifies extra data', () => {
  const input = JSON.stringify({
    cookies,
    origins: [{ origin: 'https://aistudio.google.com', localStorage: [] }],
    aistudio2api: { source: { browser: 'chrome', email: 'fixture@example.com' }, oauth: { fixture: true } },
  })
  const source = identifyCookieSource('storage-state.json', input)
  assert.equal(source.kind, 'cookies')
  assert.equal(source.format, '浏览器存储状态')
  assert.equal(source.count, 2)
  assert.equal(source.hasAccountMetadata, true)
  assert.equal(source.hasExtraMetadata, true)
  assert.equal(source.content, input)
})

test('AIS2 pairing objects are one account and preserve storage_state until server validation', () => {
  const input = JSON.stringify({
    label: 'Fixture account',
    locale: 'en-US',
    timezone: 'America/New_York',
    auth_user: 1,
    storage_state: { cookies, origins: [] },
    fingerprint: { firefox_version: 152, timezone: 'America/New_York', config: { 'navigator.userAgent': 'fixture-only' } },
  })
  const source = identifyCookieSource('account.json', input)
  assert.equal(source.kind, 'cookies')
  assert.equal(source.format, '账号凭据包')
  assert.equal(source.count, 2)
  assert.equal(source.hasAccountMetadata, true)
  assert.equal(source.hasExtraMetadata, true)
  assert.equal(source.content, input)
})

test('storageState is accepted as an alias in single-account and bundle sources', () => {
  const account = { label: 'Fixture', storageState: { cookies, origins: [] } }
  const source = identifyCookieSource('account.json', JSON.stringify(account))
  assert.equal(source.kind, 'cookies')
  assert.equal(source.count, 2)
  assert.equal(source.hasExtraMetadata, true)
  const bundle = identifyCookieSource('accounts.json', JSON.stringify([account]))
  assert.equal(bundle.kind, 'bundle')
  assert.equal(bundle.count, 1)
})

test('JSON cookie and cookies aliases retain account information', () => {
  for (const key of ['cookie', 'cookies']) {
    const input = JSON.stringify({ [key]: 'SID=fixture_sid', authUser: 1, label: 'Fixture' })
    const source = identifyCookieSource('account.json', input)
    assert.equal(source.kind, 'cookies')
    assert.equal(source.hasAccountMetadata, true)
    assert.equal(source.content, input)
  }
})

test('Netscape detection accepts HttpOnly rows but not arbitrary tabbed text', () => {
  const input = '# Netscape HTTP Cookie File\n#HttpOnly_.google.com\tTRUE\t/\tTRUE\t0\tSID\tfixture_sid'
  const source = identifyCookieSource('cookies.txt', input)
  assert.equal(source.format, 'Netscape cookies.txt')
  assert.equal(source.content, input)
  assert.throws(() => identifyCookieSource('notes.txt', 'ordinary\ttabbed text'), /无法识别格式/)
})

test('Netscape sources preserve the seventh column when the last cookie value is empty', () => {
  const emptyCookie = '.google.com\tTRUE\t/\tTRUE\t0\tSID\t'
  const input = `# Netscape HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t0\tSAPISID\tfixture_sapisid\n${emptyCookie}`
  const source = identifyCookieSource('cookies.txt', `\uFEFF \n${input}\r\n`)
  assert.equal(source.format, 'Netscape cookies.txt')
  assert.equal(source.content, input)
  assert.ok(source.content.endsWith('\t'))
  assert.equal(identifyCookieSource('empty-cookie.txt', emptyCookie).format, 'Netscape cookies.txt')
})

test('a tab after Cookie: is a header separator rather than a Netscape record', () => {
  const input = 'Cookie:\tSAPISID=fixture_sapisid; SID=fixture_sid'
  const source = identifyCookieSource('header.txt', input)
  assert.equal(source.format, 'Cookie 请求头')
  assert.equal(source.content, input)
})

test('malformed and empty containers fail with fixed messages', () => {
  assert.throws(() => identifyCookieSource('empty.json', '   '), /没有可导入的内容/)
  assert.throws(() => identifyCookieSource('invalid.json', '{"cookie":"fixture_secret"'), error => {
    assert.ok(error instanceof Error)
    assert.equal(error.message, 'JSON 格式不正确，请重新导出或检查文件')
    return true
  })
  assert.throws(() => identifyCookieSource('empty.json', '{"accounts":[]}'), /没有账号条目/)
  assert.throws(() => identifyCookieSource('invalid.json', '{"profiles":{}}'), /必须是数组/)
  assert.throws(() => identifyCookieSource('invalid.json', '{"storage_state":false}'), /必须是对象/)
})

test('credential source size is capped by UTF-8 bytes and bundle count is bounded', () => {
  const atLimit = `SID=${'x'.repeat(MAX_COOKIE_IMPORT_BYTES - 4)}`
  assert.equal(identifyCookieSource('limit.txt', atLimit).kind, 'cookies')
  assert.throws(() => identifyCookieSource('oversized.txt', `${atLimit}x`), /2 MiB/)
  assert.throws(() => identifyCookieSource('unicode.txt', `SID=${'测'.repeat(Math.floor(MAX_COOKIE_IMPORT_BYTES / 3) + 1)}`), /2 MiB/)
  const account = { cookies: 'SID=fixture_sid' }
  assert.equal(identifyCookieSource('limit.json', JSON.stringify({ accounts: Array(100).fill(account) })).count, 100)
  assert.throws(() => identifyCookieSource('oversized.json', JSON.stringify({ accounts: Array(101).fill(account) })), /最多 100 个账号/)
})

test('source identities follow content, not filenames, and expose no credentials', () => {
  const registry = createCookieSourceRegistry()
  const first = registry.identify('same-name.txt', 'SID=fixture_first')
  const renamed = registry.identify('another-name.txt', ' \nSID=fixture_first\n')
  const corrected = registry.identify('same-name.txt', 'SID=fixture_second')
  assert.equal(first.id, renamed.id)
  assert.notEqual(first.id, corrected.id)
  assert.match(first.id, /^source-\d+$/)
  assert.ok(!first.id.includes('fixture'))
  registry.clear()
  assert.equal(registry.identify('new-session.txt', 'SID=fixture_third').id, 'source-1')
})

test('partial retries keep successes across text/file selection and retry changed content', () => {
  const registry = createCookieSourceRegistry()
  const first = registry.identify('first.txt', 'SID=fixture_first')
  const failed = registry.identify('second.txt', 'SID=fixture_second')
  const results = [
    { sourceId: first.id, label: first.label, ok: true, count: 1 },
    { sourceId: failed.id, label: failed.label, ok: false, count: 0, error: 'Fixture import failure' },
  ]
  assert.deepEqual(pendingCookieSources([first, failed], results).map(source => source.id), [failed.id])
  const pastedAgain = registry.identify('粘贴内容', 'SID=fixture_first')
  assert.deepEqual(pendingCookieSources([pastedAgain], results), [])
  const corrected = registry.identify('first.txt', 'SID=fixture_corrected')
  assert.deepEqual(pendingCookieSources([first, corrected, corrected], results).map(source => source.id), [corrected.id])
})
