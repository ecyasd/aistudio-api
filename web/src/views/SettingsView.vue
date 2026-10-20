<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import { usePolling } from '@/composables/usePolling.ts'
import type { BrowserStatusResponse, SystemConfig } from '@/types/system.ts'
import ApiKeyManagerCard from '@/components/settings/ApiKeyManagerCard.vue'
import LogConfigCard from '@/components/settings/LogConfigCard.vue'
import VisualConfigEditor from '@/components/config/VisualConfigEditor.vue'
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
const browser = ref<BrowserStatusResponse | null>(null)
const browserStatusText = computed(() => {
  if (!browser.value) return '状态暂不可用'
  return {
    not_started: '等待首次调用', idle: '空闲休眠', starting: '正在启动',
    ready: '已就绪', failed: '启动失败，调用时重试', closed: '已停止，调用时启动',
  }[browser.value.status]
})
let mounted = true
let configRequest = 0
let browserRequest = 0
let configController: AbortController | undefined
let browserController: AbortController | undefined

async function loadBrowserStatus() {
  if (!mounted) return
  const request = ++browserRequest
  browserController?.abort()
  const controller = new AbortController()
  browserController = controller
  try {
    const result = await systemApi.getBrowserStatus(controller.signal)
    if (mounted && request === browserRequest) browser.value = result
  } catch {
    if (mounted && request === browserRequest) browser.value = null
  } finally {
    if (request === browserRequest) browserController = undefined
  }
}

async function loadConfig() {
  if (!mounted) return
  const request = ++configRequest
  configController?.abort()
  const controller = new AbortController()
  configController = controller
  loading.value = true
  try {
    const result = await systemApi.getConfig(controller.signal)
    if (mounted && request === configRequest) config.value = result
  } catch (err: unknown) {
    if (!mounted || request !== configRequest) return
    const msg = err instanceof Error ? err.message : '获取系统配置失败'
    toast.error(msg)
  } finally {
    if (mounted && request === configRequest) {
      loading.value = false
      configController = undefined
    }
  }
}

onMounted(() => {
  void loadConfig()
})
usePolling(loadBrowserStatus, 15000)
onUnmounted(() => {
  mounted = false
  configRequest++
  browserRequest++
  configController?.abort()
  browserController?.abort()
})
</script>

<template>
  <div class="space-y-6">
    <!-- Top Title -->
    <div>
      <h2 class="text-xl font-bold text-gray-900 tracking-tight">
        系统与模型规则配置
      </h2>
    </div>

    <!-- Server Runtime Status Cards -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div class="bg-white border border-gray-200/80 rounded-2xl p-4 shadow-xs">
        <div class="flex items-center gap-2 text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
          <Server class="w-4 h-4 text-blue-600" />
          <span>服务端口 / 浏览器</span>
        </div>
        <div class="text-base font-bold text-gray-900 font-mono">
          :{{ config?.port || 8080 }} <span class="text-xs text-gray-400 font-normal">/ CDP :{{ config?.browser_port || 9222 }}</span>
        </div>
        <div class="text-[11px] text-gray-400 mt-1">
          无头模式: {{ config?.browser_headless ? '开启 (Headless)' : '禁用' }}
        </div>
        <div
          class="text-xs mt-2"
          :class="browser?.status === 'failed' ? 'text-amber-700' : 'text-gray-600'"
        >
          浏览器：{{ browserStatusText }}
        </div>
        <div
          v-if="browser"
          class="text-[11px] text-gray-400 mt-1"
        >
          收到调用后自动启动；<template v-if="browser.idle_timeout_seconds > 0">
            空闲 {{ browser.idle_timeout_seconds }} 秒后释放内存
          </template><template v-else>
            不自动休眠
          </template>
        </div>
      </div>

      <div class="bg-white border border-gray-200/80 rounded-2xl p-4 shadow-xs">
        <div class="flex items-center gap-2 text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
          <Cpu class="w-4 h-4 text-emerald-600" />
          <span>流式响应超时</span>
        </div>
        <div class="text-base font-bold text-gray-900 font-mono">
          {{ config?.timeout_stream || 120 }}s
        </div>
        <div class="text-[11px] text-gray-400 mt-1">
          单次流式生成最大等待时间
        </div>
      </div>

      <div class="bg-white border border-gray-200/80 rounded-2xl p-4 shadow-xs">
        <div class="flex items-center gap-2 text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
          <Globe class="w-4 h-4 text-amber-600" />
          <span>网络代理状态</span>
        </div>
        <div class="text-base font-bold text-gray-900">
          <span
            v-if="config?.proxy_configured"
            class="text-emerald-600 flex items-center gap-1"
          >
            <CheckCircle2 class="w-4 h-4" /> 已配置代理
          </span>
          <span
            v-else
            class="text-gray-400 text-sm"
          >直连 (未配代理)</span>
        </div>
        <div class="text-[11px] text-gray-400 mt-1">
          支持 HTTP / SOCKS5 出口代理
        </div>
      </div>

      <div class="bg-white border border-gray-200/80 rounded-2xl p-4 shadow-xs">
        <div class="flex items-center gap-2 text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
          <Lock class="w-4 h-4 text-rose-600" />
          <span>控制台鉴权状态</span>
        </div>
        <div class="text-base font-bold">
          <span
            v-if="config?.auth_enabled"
            class="text-emerald-600 flex items-center gap-1"
          >
            <CheckCircle2 class="w-4 h-4" /> 鉴权已启用
          </span>
          <span
            v-else
            class="text-amber-600 flex items-center gap-1"
          >
            <AlertCircle class="w-4 h-4" /> 免密开放模式
          </span>
        </div>
        <div class="text-[11px] text-gray-400 mt-1">
          {{ config?.auth_enabled ? '已受 AISTUDIO_WEB_PASSWORD 保护' : '未配置 AISTUDIO_WEB_PASSWORD' }}
        </div>
      </div>
    </div>
    <!-- Restrained Auth Reminder if Auth is disabled -->
    <div
      v-if="config && !config.auth_enabled"
      class="px-4 py-3 bg-amber-50/70 border border-amber-200/80 rounded-xl flex items-center justify-between gap-3 text-xs text-amber-900"
    >
      <div class="flex items-center gap-2">
        <AlertCircle class="w-4 h-4 text-amber-600 shrink-0" />
        <span>控制台当前处于免密访问模式。公网部署建议配置环境变量 <code>AISTUDIO_WEB_PASSWORD</code> 启用鉴权。</span>
      </div>
    </div>



    <!-- Logging & Debug Config Card -->
    <LogConfigCard
      v-if="config"
      :initial-config="config"
      @saved="loadConfig"
    />
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
