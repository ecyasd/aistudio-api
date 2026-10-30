import { request } from './client.ts'
import type { ErrorRecordsQuery, ErrorRecordsResponse, ResponseDiagnostic } from '@/types/errors.ts'

function isResponseDiagnostic(value: unknown): value is ResponseDiagnostic {
  if (!value || typeof value !== 'object') return false
  const record = value as Record<string, unknown>
  const counters = ['duration_ms', 'upstream_calls', 'retries', 'body_chars', 'thought_chars',
    'thought_after_body', 'empty_thought_parts', 'marker_count']
  const inputCounters = ['request_body_bytes', 'prompt_tokens', 'thought_tokens']
  return typeof record.request_id === 'string' && typeof record.time === 'string'
    && Number.isFinite(Date.parse(record.time))
    && (record.model === null || typeof record.model === 'string')
    && typeof record.stream === 'boolean' && typeof record.auto_continue === 'boolean'
    && typeof record.outcome === 'string' && ['success', 'recovered', 'failed', 'cancelled'].includes(record.outcome)
    && (record.transport_status === null || (typeof record.transport_status === 'number' && Number.isFinite(record.transport_status)))
    && counters.every(key => typeof record[key] === 'number' && Number.isFinite(record[key]) && record[key] >= 0)
    && inputCounters.every(key => record[key] == null || (typeof record[key] === 'number' && Number.isSafeInteger(record[key]) && record[key] >= 0))
    && (record.timings_ms === undefined || (record.timings_ms !== null && typeof record.timings_ms === 'object'
      && !Array.isArray(record.timings_ms) && Object.values(record.timings_ms).every(value => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0)))
    && Array.isArray(record.finish_reasons) && record.finish_reasons.every(reason => Number.isInteger(reason))
    && Array.isArray(record.phase_sequence) && record.phase_sequence.every(phase => ['body', 'thought', 'empty_thought', 'finish'].includes(phase))
    && Array.isArray(record.flags) && record.flags.every(flag => typeof flag === 'string')
    && (record.error_kind === null || typeof record.error_kind === 'string')
}

export const errorsApi = {
  async list(query: ErrorRecordsQuery = {}, signal?: AbortSignal): Promise<ErrorRecordsResponse> {
    const params = new URLSearchParams()
    params.set('limit', String(query.limit ?? 25))
    if (query.status != null) params.set('status', String(query.status))
    if (query.category) params.set('category', query.category)
    if (query.cursor) params.set('cursor', query.cursor)
    if (query.include_diagnostics) params.set('include_diagnostics', 'true')
    const response = await request<ErrorRecordsResponse>(`/errors?${params}`, { signal })
    if (!response || !Array.isArray(response.items) || typeof response.total !== 'number'
      || !response.retention || typeof response.retention.max_records !== 'number'
      || typeof response.retention.minutes !== 'number') {
      throw new Error('错误记录响应格式无效')
    }
    if (query.include_diagnostics && (!response.diagnostics || !Array.isArray(response.diagnostics.items)
      || !response.diagnostics.items.every(isResponseDiagnostic)
      || !response.diagnostics.retention || typeof response.diagnostics.retention.max_records !== 'number'
      || typeof response.diagnostics.retention.minutes !== 'number')) {
      throw new Error('响应探测记录格式无效')
    }
    return response
  },
}
