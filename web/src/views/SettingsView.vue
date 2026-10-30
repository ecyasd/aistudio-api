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
const configError = ref('')
const sections = [
  { id: 'access', label: '接入与密钥', description: '调用密钥与控制台访问' },
  { id: 'models', label: '模型规则', description: '工具、思考与输出设置' },
  { id: 'runtime', label: '运行诊断', description: '浏览器、超时与日志' },
] as const
type Section = typeof sections[number]['id']
const activeSection = ref<Section>('access')
const sectionTabs = ref<HTMLElement | null>(null)

function onSectionKeydown(event: KeyboardEvent, index: number) {
  let next: number
  if (event.key === 'ArrowRight') next = (index + 1) % sections.length
  else if (event.key === 'ArrowLeft') next = (index + sections.length - 1) % sections.length
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = sections.length - 1
  else return
  event.preventDefault()
  const section = sections[next]
  if (!section) return
  activeSection.value = section.id
  sectionTabs.value?.querySelector<HTMLButtonElement>(`#settings-tab-${section.id}`)?.focus()
}
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
  configError.value = ''
  try {
    const result = await systemApi.getConfig(controller.signal)
    if (mounted && request === configRequest) config.value = result
  } catch (err: unknown) {
    if (!mounted || request !== configRequest) return
    const msg = err instanceof Error ? err.message : '获取系统配置失败'
    configError.value = msg
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
    <header>
      <h2 class="text-xl font-bold text-gray-900 tracking-tight">
        系统与模型配置
      </h2>
      <p class="mt-2 text-sm leading-6 text-gray-500">
        按当前要做的事选择设置，切换分区会保留未保存的编辑。
      </p>
    </header>

    <div
      ref="sectionTabs"
      role="tablist"
      aria-label="设置分区"
      class="grid grid-cols-3 gap-1 rounded-2xl border border-gray-200 bg-gray-50 p-1"
    >
      <button
        v-for="(section, index) in sections"
        :id="`settings-tab-${section.id}`"
        :key="section.id"
        type="button"
        role="tab"
        :aria-selected="activeSection === section.id"
        :aria-controls="`settings-panel-${section.id}`"
        :tabindex="activeSection === section.id ? 0 : -1"
        :class="[
          'min-h-12 min-w-0 rounded-xl px-2 py-3 text-center transition-colors cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500',
          activeSection === section.id ? 'bg-white text-brand-700 shadow-xs' : 'text-gray-600 hover:bg-white/70 hover:text-gray-900',
        ]"
        @click="activeSection = section.id"
        @keydown="onSectionKeydown($event, index)"
      >
        <span class="block text-sm font-semibold">{{ section.label }}</span>
        <span class="hidden sm:block mt-1 text-xs text-gray-500">{{ section.description }}</span>
      </button>
    </div>

    <div
      v-if="configError"
      role="alert"
      class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900"
    >
      <p>配置信息读取失败：{{ configError }}</p>
      <button
        type="button"
        class="min-h-11 rounded-lg px-3 font-semibold underline underline-offset-4 cursor-pointer disabled:opacity-50"
        :disabled="loading"
        @click="loadConfig"
      >
        重新读取
      </button>
    </div>

    <section
      v-show="activeSection === 'access'"
      id="settings-panel-access"
      role="tabpanel"
      aria-labelledby="settings-tab-access"
      tabindex="0"
      class="space-y-5 focus-visible:outline-2 focus-visible:outline-brand-500"
    >
      <div class="rounded-2xl border border-gray-200 bg-white p-5 sm:p-6">
        <div class="flex items-start gap-3">
          <Lock
            class="mt-0.5 size-5 shrink-0 text-brand-600"
            aria-hidden="true"
          />
          <div class="min-w-0">
            <h3 class="text-base font-semibold text-gray-900">
              控制台访问
            </h3>
            <p class="mt-2 text-sm leading-6 text-gray-600">
              控制台密码用于登录管理页面，API 调用密钥用于连接 SillyTavern 等客户端。
            </p>
            <p
              class="mt-3 text-sm font-medium"
              :class="config && !config.auth_enabled ? 'text-amber-700' : 'text-gray-700'"
            >
              <template v-if="!config">
                {{ loading ? '正在读取鉴权状态…' : '鉴权状态暂不可用' }}
              </template>
              <template v-else-if="config.auth_enabled">
                <CheckCircle2
                  class="mr-1 inline size-4 text-emerald-600"
                  aria-hidden="true"
                />管理密码保护已启用
              </template>
              <template v-else>
                <AlertCircle
                  class="mr-1 inline size-4"
                  aria-hidden="true"
                />未配置管理密码，管理接口已锁定
              </template>
            </p>
            <p
              v-if="config && !config.auth_enabled"
              class="mt-2 text-sm leading-6 text-amber-800"
            >
              需在服务启动配置中设置管理密码，再重新启动服务。
            </p>
          </div>
        </div>
      </div>
      <ApiKeyManagerCard />
    </section>

    <section
      v-show="activeSection === 'models'"
      id="settings-panel-models"
      role="tabpanel"
      aria-labelledby="settings-tab-models"
      tabindex="0"
      class="space-y-5 focus-visible:outline-2 focus-visible:outline-brand-500"
    >
      <div>
        <h3 class="text-base font-semibold text-gray-900">
          模型默认行为
        </h3>
        <p class="mt-2 text-sm leading-6 text-gray-500">
          集中设置模型的默认工具、思考选项和输出限制，也可切换到 YAML 编辑。
        </p>
      </div>
      <VisualConfigEditor
        v-if="config"
        :initial-yaml="config.yaml_content"
        @saved="loadConfig"
      />
      <p
        v-else
        class="rounded-xl border border-gray-200 bg-white p-5 text-sm text-gray-500"
      >
        {{ loading ? '正在读取模型规则…' : '读取配置后即可编辑模型规则。' }}
      </p>
    </section>

    <section
      v-show="activeSection === 'runtime'"
      id="settings-panel-runtime"
      role="tabpanel"
      aria-labelledby="settings-tab-runtime"
      tabindex="0"
      class="space-y-5 focus-visible:outline-2 focus-visible:outline-brand-500"
    >
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div class="rounded-2xl border border-gray-200 bg-white p-5">
          <div class="flex items-center gap-2 text-sm font-medium text-gray-500">
            <Server
              class="size-4"
              aria-hidden="true"
            />浏览器状态
          </div>
          <p
            class="mt-3 text-base font-semibold"
            :class="browser?.status === 'failed' ? 'text-amber-700' : 'text-gray-900'"
          >
            {{ browserStatusText }}
          </p>
          <p class="mt-2 text-xs leading-5 text-gray-500">
            收到调用后自动启动。<template v-if="browser && browser.idle_timeout_seconds > 0">
              空闲 {{ browser.idle_timeout_seconds }} 秒后释放内存。
            </template>
          </p>
        </div>
        <div class="rounded-2xl border border-gray-200 bg-white p-5">
          <div class="flex items-center gap-2 text-sm font-medium text-gray-500">
            <Cpu
              class="size-4"
              aria-hidden="true"
            />上游无进展超时
          </div>
          <p class="mt-3 text-base font-semibold text-gray-900">
            {{ config ? `${config.timeout_stream}s` : '暂不可用' }}
          </p>
          <p class="mt-2 text-xs leading-5 text-gray-500">
            普通流式请求在持续收到上游进展时可以继续；抗截断模式另受请求总期限约束。
          </p>
        </div>
        <div class="rounded-2xl border border-gray-200 bg-white p-5">
          <div class="flex items-center gap-2 text-sm font-medium text-gray-500">
            <Globe
              class="size-4"
              aria-hidden="true"
            />上游网络
          </div>
          <p class="mt-3 text-base font-semibold text-gray-900">
            {{ !config ? '暂不可用' : config.proxy_configured ? '出口代理已配置' : '使用直连' }}
          </p>
          <p class="mt-2 text-xs leading-5 text-gray-500">
            这是服务器访问上游的网络方式。
          </p>
        </div>
      </div>
      <details
        v-if="config"
        class="rounded-xl border border-gray-200 bg-white px-5 py-3 text-sm text-gray-600"
      >
        <summary class="min-h-8 cursor-pointer font-medium">
          服务监听信息
        </summary>
        <p class="mt-2 leading-6">
          API :{{ config.port }} · 私有 CDP :{{ config.browser_port }} · 无头浏览器{{ config.browser_headless ? '已启用' : '未启用' }}
        </p>
      </details>
      <LogConfigCard
        v-if="config"
        :initial-config="config"
        @saved="loadConfig"
      />
    </section>
  </div>
</template>
