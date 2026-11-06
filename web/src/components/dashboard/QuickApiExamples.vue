<script setup lang="ts">
import { Copy, Check, Plug } from 'lucide-vue-next'
import { useClipboard } from '@/composables/useClipboard'
const { copied, copy } = useClipboard()
const base = window.location.origin + '/v1'
</script>

<template>
  <article class="console-panel">
    <div class="panel-heading">
      <h2 class="flex items-center gap-2">
        <Plug
          :size="16"
          class="text-accent"
        />酒馆接入
      </h2><span class="text-xs text-muted">OpenAI 兼容</span>
    </div>
    <div class="connection-body">
      <div>
        <label
          for="tavern-base"
          class="text-sm text-secondary"
        >自定义 API 地址</label><div class="connection-url">
          <input
            id="tavern-base"
            :value="base"
            readonly
          ><button
            type="button"
            class="console-button"
            @click="copy(base, 0, '已复制 API 地址')"
          >
            <Check
              v-if="copied === 0"
              :size="15"
            /><Copy
              v-else
              :size="15"
            />复制地址
          </button>
        </div><p>在 SillyTavern 中选择 Chat Completion → 自定义 OpenAI 兼容接口。</p>
      </div>
      <div class="connection-options">
        <div>
          <strong>调用密钥</strong><p>使用独立 API Key，前往设置页创建或管理。</p><RouterLink
            to="/settings"
            class="text-accent text-sm"
          >管理 API Key ↗</RouterLink>
        </div><div><strong>选择模型</strong><p>普通模型走 PG；带 <code>-build</code> 后缀走 Build 代码助手。两种请求可以并发。</p></div>
      </div>
    </div>
  </article>
</template>

<style scoped>
.connection-body { padding:20px; }.connection-url { display:flex; gap:10px; margin-top:10px; }.connection-url input { flex:1; min-width:0; border:1px solid var(--color-line); border-radius:6px; padding:10px 12px; font:13px ui-monospace,monospace; }.connection-body p { color:var(--color-muted); font-size:12px; margin:9px 0; line-height:1.7; }.connection-options { display:grid; grid-template-columns:1fr 1fr; gap:24px; border-top:1px solid var(--color-line); margin-top:20px; padding-top:20px; }.connection-options strong { font-size:13px; color:var(--color-secondary); }@media(max-width:600px){.connection-options { grid-template-columns:1fr; gap:15px; }.connection-url { flex-wrap:wrap; }.connection-url input { width:100%; flex-basis:100%; }}
</style>
