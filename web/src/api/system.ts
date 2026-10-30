import { request } from './client.ts'
import type {
  StatsResponse,
  RotationStatusResponse,
  ClearCooldownRequest,
  SystemConfig,
  ApiKeyItem,
  CreateApiKeyRequest,
  UpdateApiKeyRequest,
  UpdateLoggingConfigRequest,
  UpdateLoggingConfigResponse,
  HealthCheckResponse,
  BrowserStatusResponse,
} from '@/types'


export const systemApi = {
  getBrowserStatus(signal?: AbortSignal): Promise<BrowserStatusResponse> {
    return request<BrowserStatusResponse>('/browser/status', { signal })
  },

  health(): Promise<HealthCheckResponse> {
    return request<HealthCheckResponse>('/health')
  },

  getStats(): Promise<StatsResponse> {
    return request<StatsResponse>('/stats')
  },

  getRotation(): Promise<RotationStatusResponse> {
    return request<RotationStatusResponse>('/rotation')
  },

  clearCooldown(req: ClearCooldownRequest = {}): Promise<{ ok: boolean; accounts: Record<string, unknown> }> {
    return request('/rotation/clear-cooldown', {
      method: 'POST',
      body: JSON.stringify(req),
    })
  },
  cleanupGhosts(): Promise<{ ok: boolean; cleaned_count: number; cleaned_ids: string[]; accounts: Record<string, unknown> }> {
    return request('/rotation/cleanup-ghosts', {
      method: 'POST',
    })
  },


  forceNextAccount(): Promise<{ ok: boolean; account: { id: string; name: string; email: string | null } }> {
    return request('/rotation/next', {
      method: 'POST',
    })
  },

  getConfig(signal?: AbortSignal): Promise<SystemConfig> {
    return request<SystemConfig>('/config', { signal })
  },

  updateConfigYaml(yamlContent: string, baselineYamlContent?: string): Promise<{ ok: boolean; message: string }> {
    return request('/config/yaml', {
      method: 'PUT',
      body: JSON.stringify({ yaml_content: yamlContent, baseline_yaml_content: baselineYamlContent }),
    })
  },

  updateLoggingConfig(req: UpdateLoggingConfigRequest): Promise<UpdateLoggingConfigResponse> {
    return request<UpdateLoggingConfigResponse>('/config/logging', {
      method: 'PUT',
      body: JSON.stringify(req),
    })
  },

  listApiKeys(signal?: AbortSignal): Promise<ApiKeyItem[]> {
    return request<ApiKeyItem[]>('/api-keys', { signal })
  },

  createApiKey(req: CreateApiKeyRequest): Promise<ApiKeyItem> {
    return request<ApiKeyItem>('/api-keys', {
      method: 'POST',
      body: JSON.stringify(req),
    })
  },

  deleteApiKey(keyValue: string): Promise<{ ok: boolean }> {
    return request<{ ok: boolean }>('/api-keys', {
      method: 'DELETE',
      body: JSON.stringify({ key: keyValue }),
    })
  },

  updateApiKeyName(keyValue: string, req: UpdateApiKeyRequest): Promise<ApiKeyItem> {
    return request<ApiKeyItem>('/api-keys', {
      method: 'PUT',
      body: JSON.stringify({ ...req, key: keyValue }),
    })
  },
}
