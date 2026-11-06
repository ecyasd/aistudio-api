import { request } from './client'
import type { RequestRecord } from './console'

export interface UsageStats {
  requests: number; success: number; failed: number; cancelled: number; rate_limited: number
  reported_usage: number; input_tokens: number; output_tokens: number; cached_tokens: number; total_tokens: number
  first_body_p50: number | null; first_body_p95: number | null; duration_p50: number | null; duration_p95: number | null
}
export interface UsageReport {
  from: number; to: number; bucket_seconds: number
  totals: UsageStats; previous: UsageStats
  buckets: (UsageStats & { at: number })[]
  ranking: (UsageStats & { model: string })[]
  records: RequestRecord[]; page: number; pages: number
  options: { models: string[]; channels: string[] }
  retention_days: number; max_records: number; stored_records: number
}
export const getUsage = (query: URLSearchParams, signal: AbortSignal) => request<UsageReport>(`/api/console/usage?${query}`, { signal })
