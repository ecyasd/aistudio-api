import { request } from './client'

export interface RequestRecord {
  id: string
  protocol: 'gemini' | 'openai'
  started_at: number
  model: string | null
  stream: boolean
  state: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  status: number
  error_status: number | null
  duration_ms: number
  attempts: number
  finish_reason: string | null
  body_chars: number
  thought_chars: number
  timings: Record<string, number>
  usage: Record<string, number>
  stages: { event: string; elapsed_ms: number }[]
}

export interface LogRecord {
  id: number
  time: number
  level: 'INFO' | 'WARN' | 'ERROR'
  source: string
  event: string
  message: string
  request_id: string | null
  model: string | null
  elapsed_ms: number | null
}

export const stateLabels: Record<RequestRecord['state'], string> = {
  queued: '准备中', running: '进行中', completed: '已完成', failed: '失败', cancelled: '已取消',
}
export const stageLabels: Record<string, string> = {
  received: '接收请求', preparing: '准备账号', upstream: '上游调用', retry: '重试',
  first_thought: '首个思考', first_body: '首个正文', completed: '完成', failed: '失败', cancelled: '取消',
}
export function duration(value: number | undefined): string {
  return value === undefined ? '—' : value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`
}
export function clock(value: number): string {
  return new Date(value * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}
export const cancelRequest = (id: string) => request(`/api/console/requests/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
