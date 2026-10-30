export interface SystemConfig {
  port: number
  browser_port: number
  browser_headless: boolean
  proxy_configured: boolean
  auth_enabled: boolean
  timeout_stream: number
  yaml_content: string
  log_level?: string
  dump_requests?: boolean
  debug_env_active?: boolean
  dump_to_file?: boolean
  dump_dir?: string
}
export interface UpdateLoggingConfigRequest {
  level?: string
  dump_requests?: boolean
  dump_to_file?: boolean
  dump_dir?: string
}
export interface UpdateLoggingConfigResponse {
  ok: boolean
  log_level: string
  dump_requests: boolean
  debug_env_active: boolean
  dump_to_file: boolean
  dump_dir: string
}

export interface HealthCheckResponse {
  status: string
  busy: boolean
}
export interface BrowserStatusResponse {
  status: 'not_started' | 'idle' | 'starting' | 'ready' | 'failed' | 'closed'
  process_running: boolean
  in_flight: number
  idle_timeout_seconds: number
}
export interface ApiKeyItem {
  name: string
  key: string
  created_at?: string | null
}

export interface CreateApiKeyRequest {
  name?: string
  key?: string
}

export interface UpdateApiKeyRequest {
  name: string
}
export interface ModelRulePreview {
  model: string
  auto_continue: boolean
  source: 'draft' | 'saved'
  matched_rules: { kind: 'profile' | 'model'; name: string; index?: number }[]
  ignored_generation_keys: string[]
  effective: {
    is_image_model: boolean
    default_tools: string[]
    generation_config_defaults: Record<string, unknown>
    clear_generation_config_indexes: number[]
    safety_settings: unknown[][] | null
    disable_safety_settings: boolean
    drop_unsupported_params: boolean
  }
}
