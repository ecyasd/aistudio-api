import { request } from './client.ts'

export interface UsageRecord {
  request_id: string
  time: number
  model: string | null
  channel: 'build' | 'playground'
  outcome: 'success' | 'recovered' | 'failed' | 'cancelled'
  stream: number
  duration_ms: number
  first_body_ms: number | null
  upstream_calls: number
  reported_calls: number
  usage_complete: number
  prompt_tokens: number | null
  completion_tokens: number | null
  total_tokens: number | null
  cached_tokens: number | null
}

export interface UsageReport {
  items: UsageRecord[]
  total: number
  page: number
  page_size: number
  models: string[]
  totals: {
    requests: number; success: number; failed: number; cancelled: number
    upstream_calls: number; reported_calls: number; usage_complete: number
    prompt_tokens: number | null; completion_tokens: number | null
    total_tokens: number | null; cached_tokens: number | null
  }
  retention: { max_records: number; days: number }
}

export function fetchUsage(days: number, model: string, page: number, signal: AbortSignal) {
  const query = new URLSearchParams({ days: String(days), model, page: String(page), page_size: '25' })
  return request<UsageReport>(`/requests?${query}`, { signal })
}
