<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import type { SystemConfig } from '@/types/system.ts'
import ApiKeyManagerCard from '@/components/settings/ApiKeyManagerCard.vue'
import LogConfigCard from '@/components/settings/LogConfigCard.vue'
import VisualConfigEditor from '@/components/config/VisualConfigEditor.vue'
import SignerServerCard from '@/components/settings/SignerServerCard.vue'
import RuntimeCard from '@/components/settings/RuntimeCard.vue'
import {
  Server,
  CheckCircle2,
  AlertCircle,
  Cpu,
  Globe,
  Lock,
} from 'lucide-vue-next'

const toast = useToastStore()

const config = ref<SystemConfig | null>(null)
const loading = ref(false)

async function loadConfig() {
  loading.value = true
  try {
    config.value = await systemApi.getConfig()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '获取系统配置失败'
    toast.error(msg)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadConfig()
})
</script>

<template>
  <div class="monitor-page space-y-6">
    <header class="monitor-heading">
      <div>
        <div class="eyebrow">
          WORKSPACE / SETTINGS
        </div><h1>设置</h1><p>管理运行引擎、访问密钥和模型参数。</p>
      </div>
    </header>
    <RuntimeCard @saved="loadConfig">
      <template #signers>
        <SignerServerCard
          embedded
          @switched="loadConfig"
        />
      </template>
    </RuntimeCard>

    <!-- Server Runtime Status Cards -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div class="bg-panel border border-line/80 rounded-2xl p-4 ">
        <div class="flex items-center gap-2 text-xs font-semibold text-muted uppercase tracking-wider mb-2">
          <Server class="w-4 h-4 text-info" />
          <span>服务端口</span>
        </div>
        <div class="text-base font-semibold text-fg font-mono">
          :{{ config?.port || 8080 }}
        </div>
        <div class="text-[11px] text-muted mt-1">
          HTTP 服务监听端口
        </div>
      </div>

      <div class="bg-panel border border-line/80 rounded-2xl p-4 ">
        <div class="flex items-center gap-2 text-xs font-semibold text-muted uppercase tracking-wider mb-2">
          <Cpu class="w-4 h-4 text-success" />
          <span>日志输出模式</span>
        </div>
        <div class="text-base font-semibold text-fg font-mono">
          {{ config?.log_level || 'INFO' }}
        </div>
        <div class="text-[11px] text-muted mt-1">
          诊断记录: 有界脱敏元数据
        </div>
      </div>

      <div class="bg-panel border border-line/80 rounded-2xl p-4 ">
        <div class="flex items-center gap-2 text-xs font-semibold text-muted uppercase tracking-wider mb-2">
          <Globe class="w-4 h-4 text-warning" />
          <span>网络代理状态</span>
        </div>
        <div class="text-base font-semibold text-fg">
          <span
            v-if="config?.proxy_configured"
            class="text-success flex items-center gap-1"
          >
            <CheckCircle2 class="w-4 h-4" /> 已配置代理
          </span>
          <span
            v-else
            class="text-muted text-sm"
          >直连 (未配代理)</span>
        </div>
        <div class="text-[11px] text-muted mt-1">
          支持 HTTP / SOCKS5 出口代理
        </div>
      </div>

      <div class="bg-panel border border-line/80 rounded-2xl p-4 ">
        <div class="flex items-center gap-2 text-xs font-semibold text-muted uppercase tracking-wider mb-2">
          <Lock class="w-4 h-4 text-danger" />
          <span>控制台鉴权状态</span>
        </div>
        <div class="text-base font-semibold">
          <span
            v-if="config?.auth_enabled"
            class="text-success flex items-center gap-1"
          >
            <CheckCircle2 class="w-4 h-4" /> 鉴权已启用
          </span>
          <span
            v-else
            class="text-warning flex items-center gap-1"
          >
            <AlertCircle class="w-4 h-4" /> 免密开放模式
          </span>
        </div>
        <div class="text-[11px] text-muted mt-1">
          {{ config?.auth_enabled ? '已受 AISTUDIO_WEB_PASSWORD 保护' : '未配置 AISTUDIO_WEB_PASSWORD' }}
        </div>
      </div>
    </div>
    <!-- Restrained Auth Reminder if Auth is disabled -->
    <div
      v-if="config && !config.auth_enabled"
      class="px-4 py-3 bg-warning-soft/70 border border-warning-line/80 rounded-xl flex items-center justify-between gap-3 text-xs text-warning"
    >
      <div class="flex items-center gap-2">
        <AlertCircle class="w-4 h-4 text-warning shrink-0" />
        <span>控制台当前处于免密访问模式。公网部署建议配置环境变量 <code>AISTUDIO_WEB_PASSWORD</code> 启用鉴权。</span>
      </div>
    </div>



    <!-- Logging & Debug Config Card -->
    <LogConfigCard
      v-if="config"
      :initial-config="config"
      @saved="loadConfig"
    />
    <!-- WAA Signer Servers & Features Binding Card -->

    <!-- API Key Management Card -->
    <ApiKeyManagerCard />

    <!-- Visual & YAML Model Config Editor -->
    <VisualConfigEditor
      v-if="config"
      :initial-yaml="config.yaml_content"
      @saved="loadConfig"
    />
  </div>
</template>
