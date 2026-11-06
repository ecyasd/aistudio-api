<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { FileText, Upload, X, CheckCircle2 } from 'lucide-vue-next'
import { useAccountsStore } from '@/stores/accounts'
import { createCookieSourceRegistry, pendingCookieSources, MAX_COOKIE_IMPORT_BYTES, type CookieSource, type CookieImportResult } from '@/utils/cookieImport'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const store = useAccountsStore()
const sourceRegistry = createCookieSourceRegistry()
const tab = ref<'text' | 'files'>('text')
const text = ref(''), name = ref(''), authUser = ref(''), discover = ref(false), error = ref(''), reading = ref(false), dragging = ref(false)
const files = ref<{ id: string; name: string; size: number; source?: CookieSource; error?: string }[]>([])
const results = ref<CookieImportResult[]>([])
const picker = ref<HTMLInputElement>()
let revision = 0, readSequence = 0
watch(() => props.modelValue, () => {
  revision++; readSequence++
  text.value = ''; files.value = []; results.value = []; error.value = ''; reading.value = false
  name.value = ''; authUser.value = ''; discover.value = false; dragging.value = false
  sourceRegistry.clear()
}, { flush: 'sync' })
onUnmounted(() => { revision++; readSequence++; sourceRegistry.clear() })
const pasted = computed(() => {
  if (!text.value.trim()) return { source: undefined, error: '' }
  try { return { source: sourceRegistry.identify('粘贴内容', text.value), error: '' } }
  catch (cause) { return { source: undefined, error: cause instanceof Error ? cause.message : '无法识别格式' } }
})
const sources = computed(() => {
  const selected = tab.value === 'text' ? (pasted.value.source ? [pasted.value.source] : []) : files.value.flatMap(item => item.source ? [item.source] : [])
  return Array.from(new Map(selected.map(source => [source.id, source])).values())
})
const remaining = computed(() => pendingCookieSources(sources.value, results.value))
const visibleResults = computed(() => sources.value.flatMap(source => {
  const result = results.value.find(item => item.sourceId === source.id)
  return result ? [{ ...result, label: source.label }] : []
}))
const hasExtraMetadata = computed(() => sources.value.some(source => source.hasExtraMetadata))
const canChooseIdentity = computed(() => !discover.value && (!sources.value.length || sources.value.some(source => source.kind === 'cookies')))
const authUserError = computed(() => canChooseIdentity.value && authUser.value.trim() && (!/^\d+$/.test(authUser.value.trim()) || Number(authUser.value.trim()) > 999) ? '登录身份序号须为 0 到 999 的整数' : '')
watch([text, tab], () => { error.value = '' })
const hasErrors = computed(() => tab.value === 'text' ? !!pasted.value.error : files.value.some(item => !!item.error))
async function readFiles(selected: File[]) {
  if (!selected.length || store.importing) return
  const sequence = ++readSequence, current = revision
  reading.value = true; error.value = ''
  const entries = await Promise.all(selected.map(async (file, index) => {
    const label = selected.filter(item => item.name === file.name).length > 1 ? `${file.name} (${index + 1})` : file.name
    const entry = { id: `file-${sequence}-${index}`, name: label, size: file.size }
    try {
      if (file.size > MAX_COOKIE_IMPORT_BYTES) throw new Error('文件不能超过 2 MiB，请缩小文件或拆分账号包')
      const content = await file.text()
      if (sequence !== readSequence || current !== revision) return entry
      return { ...entry, source: sourceRegistry.identify(label, content) }
    }
    catch (cause) { return { ...entry, error: cause instanceof Error ? cause.message : '文件读取失败，请重新选择' } }
  }))
  if (sequence !== readSequence || current !== revision) return
  files.value = entries; reading.value = false
}
function choose(event: Event) {
  const input = event.target as HTMLInputElement
  const selected = Array.from(input.files ?? [])
  input.value = ''
  void readFiles(selected)
}
function drop(event: DragEvent) { dragging.value = false; if (!store.importing) void readFiles(Array.from(event.dataTransfer?.files ?? [])) }
function updateOpen(value: boolean) { if (!store.importing) emit('update:modelValue', value) }
async function submit() {
  if (store.importing || reading.value || hasErrors.value || authUserError.value || !remaining.value.length) return
  const current = revision
  const imported = await store.importSources(remaining.value, { discover: discover.value, name: name.value.trim(), authUser: authUser.value.trim() || undefined })
  if (current !== revision || !props.modelValue) return
  const attemptedIds = new Set(imported.map(item => item.sourceId))
  results.value = [...results.value.filter(item => !attemptedIds.has(item.sourceId)), ...imported]
  if (imported.length && imported.every(item => item.ok)) emit('update:modelValue', false)
  else error.value = '导入未完成，请修正或重试失败来源；已成功的相同内容不会重复提交。'
}
const describe = (source: CookieSource) => `${source.format}${source.count ? ` · ${source.count} ${source.kind === 'bundle' ? '个账号条目' : '条 Cookie 记录'}` : ''}`
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="导入账号"
    max-width="xl"
    @update:model-value="updateOpen"
  >
    <div class="space-y-5">
      <p class="text-sm text-muted">
        粘贴 Cookie，或选择凭据文件。自动识别格式，默认仅保存，不联网识别账号。
      </p>
      <div
        class="import-tabs"
        role="tablist"
        aria-label="导入方式"
      >
        <button
          type="button"
          role="tab"
          :aria-selected="tab === 'text'"
          :disabled="store.importing || reading"
          @click="tab = 'text'"
        >
          <FileText :size="16" />粘贴文本
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="tab === 'files'"
          :disabled="store.importing || reading"
          @click="tab = 'files'"
        >
          <Upload :size="16" />上传文件
        </button>
      </div>
      <div
        v-if="tab === 'text'"
        class="space-y-2"
      >
        <label
          for="import-cookie-text"
          class="text-sm text-secondary"
        >Cookie 内容</label>
        <textarea
          id="import-cookie-text"
          v-model="text"
          :disabled="store.importing"
          rows="7"
          spellcheck="false"
          autocapitalize="off"
          autocomplete="off"
          class="import-text"
          placeholder="支持 Cookie 请求头、JSON、storage-state.json、Netscape cookies.txt、账号包或含 storage_state 的凭据包"
        />
        <p
          v-if="pasted.source"
          class="text-sm text-success"
          role="status"
        >
          {{ describe(pasted.source) }}
        </p>
        <p
          v-else-if="pasted.error"
          class="text-sm text-danger"
          role="alert"
        >
          {{ pasted.error }}
        </p>
      </div>
      <div
        v-else
        class="space-y-3"
      >
        <label
          class="import-drop"
          :class="{ dragging }"
          @dragover.prevent="dragging = true"
          @dragleave.prevent="dragging = false"
          @drop.prevent="drop"
        >
          <Upload :size="24" /><strong>{{ reading ? '正在读取文件…' : '选择或拖入文件' }}</strong><span>可多选 .json / .txt；每个文件最多 2 MiB、100 个账号</span>
          <input
            ref="picker"
            type="file"
            multiple
            accept=".json,.txt,application/json,text/plain"
            :disabled="store.importing || reading"
            aria-label="选择 Cookie 文件"
            @change="choose"
          >
        </label>
        <div
          v-for="(file, index) in files"
          :key="file.id"
          class="import-source"
        >
          <FileText :size="17" /><div><strong>{{ file.name }}</strong><small>{{ file.source ? describe(file.source) : file.error }} · {{ (file.size / 1024).toFixed(1) }} KB</small></div><button
            type="button"
            :disabled="store.importing"
            :aria-label="`移除 ${file.name}`"
            @click="files.splice(index, 1)"
          >
            <X :size="16" />
          </button>
        </div>
      </div>
      <p
        v-if="hasExtraMetadata"
        class="import-notice"
        role="status"
      >
        仅导入兼容的 Cookie 和账号信息。时区、语言及浏览器指纹沿用服务设置；凭据包中的 OAuth 和站点存储不会保存。
      </p>
      <div class="space-y-2">
        <label
          for="import-account-name"
          class="text-sm text-secondary"
        >账号名称或前缀 <span class="text-muted">（可选）</span></label><input
          id="import-account-name"
          v-model="name"
          :disabled="store.importing"
          class="import-text"
          placeholder="例如：日常使用"
        >
      </div>
      <div
        v-if="canChooseIdentity"
        class="space-y-2"
      >
        <label
          for="import-auth-user"
          class="text-sm text-secondary"
        >登录身份 u/N <span class="text-muted">（可选）</span></label>
        <input
          id="import-auth-user"
          v-model="authUser"
          type="text"
          inputmode="numeric"
          autocomplete="off"
          :disabled="store.importing"
          :aria-invalid="!!authUserError"
          aria-describedby="import-auth-user-help"
          class="import-text"
          placeholder="留空沿用文件信息，缺省为 0"
        >
        <p
          id="import-auth-user-help"
          class="text-xs text-muted"
        >
          仅对单账号来源生效。纯 Cookie 无法判断身份序号；需要 u/1 等身份时可填写，或开启下方的多个登录身份识别。账号包沿用各条目的身份。
        </p>
        <p
          v-if="authUserError"
          class="text-sm text-danger"
          role="alert"
        >
          {{ authUserError }}
        </p>
      </div>
      <label class="import-discovery"><input
        v-model="discover"
        type="checkbox"
        :disabled="store.importing"
      ><div><strong>识别并导入 Cookie 中的多个登录身份</strong><small>开启后会访问 Google，识别 u/0、u/1 等身份；账号包按文件内条目导入。仅保存时，可在导入后从账号行识别邮箱和订阅。</small></div></label>
      <div
        v-if="visibleResults.length"
        class="space-y-2"
      >
        <p
          v-for="item in visibleResults"
          :key="item.sourceId"
          class="text-sm"
          :class="item.ok ? 'text-success' : 'text-danger'"
        >
          <CheckCircle2
            v-if="item.ok"
            :size="14"
            class="inline"
          />{{ item.label }}：{{ item.ok ? `已保存 ${item.count} 个账号` : item.error }}
        </p>
      </div>
      <p
        v-if="error"
        class="text-sm text-danger"
        role="alert"
      >
        {{ error }}
      </p>
    </div>
    <template #footer>
      <Button
        variant="ghost"
        :disabled="store.importing"
        @click="updateOpen(false)"
      >
        取消
      </Button><Button
        variant="primary"
        :loading="store.importing"
        :disabled="reading || hasErrors || !!authUserError || !remaining.length"
        @click="submit"
      >
        {{ discover ? '识别并导入' : '导入保存' }}
      </Button>
    </template>
  </Modal>
</template>

<style scoped>
.import-tabs { display:flex; gap:6px; border-bottom:1px solid var(--color-line); padding-bottom:10px; }.import-tabs button { display:flex; align-items:center; gap:8px; padding:9px 14px; border-radius:6px; color:var(--color-muted); }.import-tabs button[aria-selected='true'] { background:var(--color-accent-soft); color:var(--color-accent); }
.import-text { display:block; width:100%; border:1px solid var(--color-line); border-radius:6px; padding:10px 12px; font-size:13px; line-height:1.6; }.import-text:is(textarea) { font-family:ui-monospace,monospace; resize:vertical; }
.import-drop { display:flex; align-items:center; flex-direction:column; gap:8px; position:relative; border:1px dashed var(--color-line-strong); border-radius:8px; padding:28px 16px; color:var(--color-muted); cursor:pointer; }.import-drop input { position:absolute; inset:0; opacity:0; width:100%; height:100%; cursor:pointer; }.import-drop strong { color:var(--color-fg); font-size:14px; }.import-drop span { font-size:12px; }.import-drop:focus-within, .import-drop.dragging { border-color:var(--color-accent); outline:2px solid var(--color-accent); outline-offset:3px; }
.import-source { display:flex; align-items:center; gap:10px; padding:12px; border:1px solid var(--color-line); border-radius:6px; }.import-source > div { min-width:0; flex:1; }.import-source strong { display:block; font-size:13px; overflow-wrap:anywhere; }.import-source small { display:block; color:var(--color-muted); font-size:12px; margin-top:4px; }.import-source button { padding:8px; }
.import-discovery { display:flex; align-items:flex-start; gap:10px; border:1px solid var(--color-line); border-radius:6px; padding:14px; }.import-discovery input { margin-top:4px; }.import-discovery strong { display:block; color:var(--color-secondary); font-size:13px; }.import-discovery small { display:block; color:var(--color-muted); margin-top:5px; font-size:12px; line-height:1.6; }
.import-notice { border:1px solid var(--color-line); border-radius:6px; padding:12px; color:var(--color-muted); font-size:12px; line-height:1.7; }
</style>
