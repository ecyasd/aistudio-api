export interface CookieSource {
  label: string
  content: string
  kind: 'cookies' | 'bundle'
  format: string
  count?: number
}
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const serializeCookies = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(object(value) && Object.values(value).every(item => typeof item === 'string') ? { cookies: value } : value)

export function identifyCookieSource(label: string, input: string): CookieSource {
  const content = input.replace(/^\uFEFF/, '').trim()
  if (!content) throw new Error('没有可导入的内容')
  if (content.startsWith('{') || content.startsWith('[')) {
    let value: unknown
    try { value = JSON.parse(content) } catch { throw new Error('JSON 格式不正确，请重新导出或检查文件') }
    const entries = object(value) ? value.accounts ?? value.profiles : value
    if (Array.isArray(entries) && entries.length && entries.every(item => object(item) && ('cookies' in item || 'storage_state' in item))) {
      const accounts = entries.map(item => {
        const cookies = item.cookies ?? item.storage_state
        return { name: item.name ?? item.label, email: item.email, auth_user: String(item.auth_user ?? item.authUser ?? '0'), tier: item.tier ?? item.g1Tier, cookies: serializeCookies(cookies) }
      })
      return { label, kind: 'bundle', content: JSON.stringify({ accounts }), count: accounts.length, format: '账号包' }
    }
    const cookies = Array.isArray(value) ? value : object(value) ? value.cookies : undefined
    if (Array.isArray(cookies) && cookies.length && cookies.every(item => object(item) && typeof item.name === 'string' && typeof item.value === 'string')) {
      return { label, content, kind: 'cookies', format: 'Cookie JSON', count: cookies.length }
    }
    if (object(cookies) && Object.keys(cookies).length && Object.values(cookies).every(item => typeof item === 'string')) {
      return { label, content, kind: 'cookies', format: 'Cookie 键值对象', count: Object.keys(cookies).length }
    }
    if (object(value) && (typeof value.cookie === 'string' || typeof value.cookies === 'string')) {
      return { label, content, kind: 'cookies', format: 'Cookie JSON' }
    }
    if (object(value) && Object.values(value).every(item => typeof item === 'string') && Object.keys(value).some(key => /SID|SAPISID/.test(key))) {
      return { label, content: JSON.stringify({ cookies: value }), kind: 'cookies', format: 'Cookie 键值对象', count: Object.keys(value).length }
    }
    throw new Error('未识别到 Cookie 数组、storage state 或账号包')
  }
  if (content.includes('\t')) return { label, content, kind: 'cookies', format: 'Netscape cookies.txt' }
  if (/\b[^;=\s]+\s*=/.test(content)) return { label, content, kind: 'cookies', format: 'Cookie 请求头' }
  throw new Error('无法识别格式，请使用 Cookie 请求头、JSON 或 cookies.txt')
}

export interface CookieImportResult { label: string; ok: boolean; count: number; error?: string }
