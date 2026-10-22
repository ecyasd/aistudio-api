export interface ErrorFrame {
  file: string
  line: number
  function: string
}

export interface ErrorRecord {
  id: string
  request_id: string
  time: string
  status: number
  transport_status: number | null
  category: string
  code?: string
  summary: string
  exception_type?: string | null
  frames: ErrorFrame[]
  source: string
  operation: string
  model?: string | null
  account_id?: string | null
  attempt?: number | null
  retrying: boolean
  outcome: 'retrying' | 'failed' | 'recovered' | 'cancelled' | 'warning'
}

export interface ResponseDiagnostic {
  request_id: string
  time: string
  model: string | null
  stream: boolean
  auto_continue: boolean
  outcome: 'success' | 'recovered' | 'failed' | 'cancelled'
  transport_status: number | null
  duration_ms: number
  upstream_calls: number
  retries: number
  finish_reasons: number[]
  body_chars: number
  thought_chars: number
  thought_after_body: number
  empty_thought_parts: number
  marker_count: number
  phase_sequence: ('body' | 'thought' | 'empty_thought' | 'finish')[]
  flags: string[]
  error_kind: string | null
}

export interface ErrorRecordsResponse {
  items: ErrorRecord[]
  total: number
  has_more: boolean
  next_cursor: string | null
  retention: { max_records: number; minutes: number }
  diagnostics?: {
    items: ResponseDiagnostic[]
    retention: { max_records: number; minutes: number }
  }
}

export interface ErrorRecordsQuery {
  limit?: number
  status?: number
  category?: string
  cursor?: string | null
  include_diagnostics?: boolean
}
