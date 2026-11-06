<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { Power, Save } from 'lucide-vue-next'
import { request } from '@/api/client'

interface RuntimeState { saved: { engine: 'browserless' | 'browser' }; active: { engine: string }; restart_required: boolean; restart_enabled: boolean; instance_id: string }
const state = ref<RuntimeState>()
const engine = ref('browserless'), error = ref(''), notice = ref(''), busy = ref(false), confirm = ref(false)
const controller = new AbortController()
const emit = defineEmits<{ saved: [] }>()
async function load() { state.value = await request<RuntimeState>('/api/console/runtime', { signal: controller.signal }); engine.value = state.value.saved.engine }
onMounted(() => { void load().catch(cause => { if (!controller.signal.aborted) error.value = String(cause) }) })
onUnmounted(() => controller.abort())
async function save() {
  busy.value = true; error.value = ''; notice.value = ''
  try { state.value = await request('/api/console/runtime', { method: 'PUT', body: JSON.stringify({ engine: engine.value }), signal: controller.signal }); notice.value = '引擎配置已保存，重启服务后生效'; emit('saved') }
  catch (cause) { if (!controller.signal.aborted) error.value = cause instanceof Error ? cause.message : '保存失败' }
  finally { busy.value = false }
}
async function restart() {
  const before = state.value?.instance_id
  confirm.value = false; busy.value = true; error.value = ''; notice.value = '正在重启，等待服务恢复…'
  try {
    await request('/api/console/restart', { method: 'POST', signal: controller.signal })
    for (let attempt = 0; attempt < 45; attempt++) {
      await new Promise<void>(resolve => setTimeout(resolve, 2000))
      if (controller.signal.aborted) return
      try { await load(); if (state.value?.instance_id !== before) { notice.value = '服务已恢复，配置已生效'; return } } catch { /* Container may be restarting. */ }
    }
    throw new Error('服务尚未恢复，请稍后刷新；服务器命令见部署文档')
  } catch (cause) { if (!controller.signal.aborted) { error.value = cause instanceof Error ? cause.message : '重启失败'; notice.value = '' } }
  finally { busy.value = false }
}
</script>

<template>
  <article class="console-panel runtime-card monitor-page">
    <div class="panel-heading">
      <h2>运行引擎</h2><span class="muted-text">当前：{{ state?.active.engine === 'browser' ? 'CloakBrowser' : '无浏览器' }}</span>
    </div>
    <div class="runtime-body">
      <label for="runtime-engine">运行引擎</label><select
        id="runtime-engine"
        v-model="engine"
        :disabled="busy || !state"
      >
        <option value="browserless">
          无浏览器（默认）
        </option><option value="browser">
          有浏览器（CloakBrowser）
        </option>
      </select>
      <button
        type="button"
        class="console-button"
        :disabled="busy || !state"
        @click="save"
      >
        <Save :size="15" />保存引擎
      </button>
      <p class="muted-text">
        PG 与 Build 代码助手同时开放，普通模型走 PG，带 -build 的模型走代码助手。引擎切换需要重启。
      </p>
      <div
        v-show="engine === 'browserless'"
        class="runtime-signers"
      >
        <slot name="signers" />
      </div>
      <p
        v-if="engine === 'browser'"
        class="muted-text"
      >
        CloakBrowser 在本机生成证明并发送请求，无需选择 WAA 签名服务。
      </p>
      <div
        v-if="state?.restart_required"
        class="runtime-notice"
      >
        存在等待重启生效的引擎配置。
      </div>
      <div class="runtime-restart">
        <div>
          <strong>重启服务</strong><p class="muted-text">
            重新加载配置；账号、调用密钥、统计与日志保留。正在生成时请先等待或停止请求。
          </p>
        </div><button
          type="button"
          class="console-button"
          :disabled="busy || !state?.restart_enabled"
          @click="confirm = true"
        >
          <Power :size="15" />重启服务
        </button>
      </div>
      <div
        v-if="confirm"
        class="runtime-confirm"
        role="alertdialog"
        aria-label="确认重启服务"
      >
        <p>现在重启服务？控制台会短暂断开并自动等待恢复。</p><div>
          <button
            type="button"
            class="console-button"
            @click="confirm = false"
          >
            取消
          </button><button
            type="button"
            class="console-button"
            @click="restart"
          >
            确认重启
          </button>
        </div>
      </div>
      <p
        v-if="!state?.restart_enabled && state"
        class="muted-text"
      >
        当前运行环境未启用重启按钮。
      </p>
      <p
        v-if="notice"
        class="accent-text"
        role="status"
      >
        {{ notice }}
      </p><p
        v-if="error"
        class="console-error"
        role="alert"
      >
        {{ error }}
      </p>
      <details>
        <summary class="muted-text">
          服务器备用命令
        </summary><code class="restart-command">sudo systemctl restart aistudio-api.service</code>
      </details>
    </div>
  </article>
</template>

<style scoped>
.runtime-body { padding: 20px; display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }.runtime-body > p, .runtime-body > details, .runtime-notice, .runtime-confirm, .runtime-restart { width: 100%; }.runtime-body label { font-size: 13px; }.runtime-restart { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 18px; padding-top: 20px; border-top: 1px solid var(--console-line); }.runtime-restart strong { display: block; margin-bottom: 6px; }.runtime-restart > div { flex: 1; min-width: 210px; }.runtime-notice { color: #e5b96a; }.runtime-confirm { padding: 18px; border: 1px solid #e5b96a; border-radius: 6px; }.runtime-confirm > div { display: flex; justify-content: flex-end; gap: 10px; margin-top: 14px; }.restart-command { display: block; padding: 14px; margin-top: 10px; background: var(--console-bg); overflow-wrap: anywhere; }.runtime-body :is(.muted-text, .console-error) { margin: 0; }
</style>

<style scoped>
.runtime-signers { width: 100%; border-top: 1px solid var(--console-line); padding-top: 18px; }
</style>
