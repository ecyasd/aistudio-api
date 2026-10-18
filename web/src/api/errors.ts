import { request } from './client.ts'
import type { ErrorRecordsQuery, ErrorRecordsResponse } from '@/types/errors.ts'

export const errorsApi = {
  async list(query: ErrorRecordsQuery = {}, signal?: AbortSignal): Promise<ErrorRecordsResponse> {
    const params = new URLSearchParams()
    params.set('limit', String(query.limit ?? 25))
    if (query.status != null) params.set('status', String(query.status))
    if (query.category) params.set('category', query.category)
    if (query.cursor) params.set('cursor', query.cursor)
    const response = await request<ErrorRecordsResponse>(`/errors?${params}`, { signal })
    if (!response || !Array.isArray(response.items) || typeof response.total !== 'number'
      || !response.retention || typeof response.retention.max_records !== 'number'
      || typeof response.retention.minutes !== 'number') {
      throw new Error('错误记录响应格式无效')
    }
    return response
  },
}
