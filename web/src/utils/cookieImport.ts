interface CookieSourceDetails {
  label: string
  content: string
  kind: 'cookies' | 'bundle'
  format: string
  count?: number
  hasAccountMetadata?: boolean
  hasExtraMetadata?: boolean
}

export interface CookieSource extends CookieSourceDetails { id: string }

export const MAX_COOKIE_IMPORT_BYTES = 2 * 1024 * 1024
export const MAX_COOKIE_BUNDLE_ACCOUNTS = 100

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)

const hasAccountMetadata = (value: Record<string, unknown>) => ['id', 'account_id', 'name', 'label', 'email', 'auth_user', 'authUser', 'tier', 'g1Tier', 'aistudio2api'].some(key => key in value)
const hasExtraMetadata = (value: Record<string, unknown>): boolean =>
  ['fingerprint', 'timezone', 'locale', 'user_agent', 'userAgent', 'origins', 'aistudio2api'].some(key => key in value)
  || (object(value.storage_state) && hasExtraMetadata(value.storage_state))
  || (object(value.storageState) && hasExtraMetadata(value.storageState))

function cookieCount(value: unknown): number | undefined {
  if (Array.isArray(value)) return value.length
  if (!object(value)) return undefined
  if ('storage_state' in value) return cookieCount(value.storage_state)
  if ('storageState' in value) return cookieCount(value.storageState)
  if ('cookies' in value) return cookieCount(value.cookies)
  if ('cookie' in value) return cookieCount(value.cookie)
  return Object.values(value).every(item => typeof item === 'string') ? Object.keys(value).length : undefined
}

/** Classify locally; the server validates and normalizes the unchanged source. */
export function identifyCookieSource(label: string, input: string): CookieSourceDetails {
  const content = input.replace(/^\uFEFF/, '').replace(/^[ \r\n]+|[ \r\n]+$/g, '')
  if (!content.trim()) throw new Error('没有可导入的内容')
  if (content.length > MAX_COOKIE_IMPORT_BYTES || new TextEncoder().encode(content).length > MAX_COOKIE_IMPORT_BYTES) {
    throw new Error('每个凭据来源不能超过 2 MiB，请缩小文件或拆分账号包')
  }
  if (content.startsWith('{') || content.startsWith('[')) {
    let value: unknown
    try { value = JSON.parse(content) } catch { throw new Error('JSON 格式不正确，请重新导出或检查文件') }

    const wrappedBundle = object(value) && ('accounts' in value || 'profiles' in value)
    const entries = object(value) ? value.accounts ?? value.profiles : value
    if (wrappedBundle || (Array.isArray(entries) && entries.some(item => object(item) && ('cookies' in item || 'cookie' in item || 'storage_state' in item || 'storageState' in item)))) {
      if (!Array.isArray(entries)) throw new Error('账号包中的 accounts 或 profiles 必须是数组')
      if (!entries.length) throw new Error('账号包内没有账号条目')
      if (entries.length > MAX_COOKIE_BUNDLE_ACCOUNTS) throw new Error('每个账号包最多 100 个账号，请拆分后导入')
      return {
        label, content, kind: 'bundle', format: '账号包', count: entries.length,
        hasExtraMetadata: (object(value) && hasExtraMetadata(value)) || entries.some(item => object(item) && hasExtraMetadata(item)),
      }
    }

    if (object(value) && ('storage_state' in value || 'storageState' in value)) {
      const storageState = value.storage_state ?? value.storageState
      if (!object(storageState)) throw new Error('账号凭据包中的 storage_state 必须是对象')
      return {
        label, content, kind: 'cookies', format: '账号凭据包', count: cookieCount(storageState),
        hasAccountMetadata: true, hasExtraMetadata: hasExtraMetadata(value),
      }
    }

    const metadata = object(value)
      ? { hasAccountMetadata: hasAccountMetadata(value), hasExtraMetadata: hasExtraMetadata(value) }
      : {}
    const cookies = Array.isArray(value) ? value : object(value) ? value.cookies ?? value.cookie : undefined
    if (Array.isArray(cookies) && cookies.length && cookies.every(item => object(item) && typeof item.name === 'string' && typeof item.value === 'string')) {
      const format = object(value) && ('origins' in value || 'aistudio2api' in value) ? '浏览器存储状态' : 'Cookie JSON'
      return { label, content, kind: 'cookies', format, count: cookies.length, ...metadata }
    }
    if (object(cookies) && Object.keys(cookies).length && Object.values(cookies).every(item => typeof item === 'string')) {
      return { label, content, kind: 'cookies', format: 'Cookie 键值对象', count: Object.keys(cookies).length, ...metadata }
    }
    if (object(value) && (typeof value.cookie === 'string' || typeof value.cookies === 'string')) {
      return { label, content, kind: 'cookies', format: 'Cookie JSON', ...metadata }
    }
    if (object(value) && Object.values(value).every(item => typeof item === 'string') && Object.keys(value).some(key => /SID|SAPISID/.test(key))) {
      return { label, content, kind: 'cookies', format: 'Cookie 键值对象', count: Object.keys(value).length }
    }
    throw new Error('未识别到 Cookie 数组、storage state 或账号包')
  }
  if (content.split(/\r?\n/).some(line => {
    const record = line.replace(/^[ \r\n]+|[ \r\n]+$/g, '').replace(/^#HttpOnly_/, '')
    return !record.startsWith('#') && record.split('\t').length === 7
  })) return { label, content, kind: 'cookies', format: 'Netscape cookies.txt' }
  if (/(?:^|[;\r\n])\s*(?:cookie:\s*)?[^;=\s]+\s*=/i.test(content)) return { label, content, kind: 'cookies', format: 'Cookie 请求头' }
  throw new Error('无法识别格式，请使用 Cookie 请求头、JSON 或 cookies.txt')
}

/** Keep exact source identity in memory for one dialog, without exposing credentials in UI keys. */
export function createCookieSourceRegistry() {
  const identities = new Map<string, string>()
  let sequence = 0
  return {
    identify(label: string, input: string): CookieSource {
      const source = identifyCookieSource(label, input)
      let id = identities.get(source.content)
      if (!id) {
        id = `source-${++sequence}`
        identities.set(source.content, id)
      }
      return { ...source, id }
    },
    clear() { identities.clear(); sequence = 0 },
  }
}

export interface CookieImportResult { sourceId: string; label: string; ok: boolean; count: number; error?: string }

export function pendingCookieSources(sources: CookieSource[], results: CookieImportResult[]): CookieSource[] {
  const completed = new Set(results.filter(result => result.ok).map(result => result.sourceId))
  return Array.from(new Map(sources.map(source => [source.id, source])).values())
    .filter(source => !completed.has(source.id))
}
