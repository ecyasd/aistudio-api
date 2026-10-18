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

export interface ErrorRecordsResponse {
  items: ErrorRecord[]
  total: number
  has_more: boolean
  next_cursor: string | null
  retention: { max_records: number; minutes: number }
}

export interface ErrorRecordsQuery {
  limit?: number
  status?: number
  category?: string
  cursor?: string | null
}
