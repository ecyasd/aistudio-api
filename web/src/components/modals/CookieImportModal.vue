<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { FileText, Upload, X, CheckCircle2 } from 'lucide-vue-next'
import { useAccountsStore } from '@/stores/accounts'
import { identifyCookieSource, type CookieSource, type CookieImportResult } from '@/utils/cookieImport'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const store = useAccountsStore()
const tab = ref<'text' | 'files'>('text')
const text = ref(''), name = ref(''), discover = ref(false), error = ref(''), reading = ref(false), dragging = ref(false)
const files = ref<{ name: string; size: number; source?: CookieSource; error?: string }[]>([])
const results = ref<CookieImportResult[]>([])
const picker = ref<HTMLInputElement>()
let revision = 0, readSequence = 0
watch(() => props.modelValue, () => {
  revision++; readSequence++
  text.value = ''; files.value = []; results.value = []; error.value = ''; reading.value = false
  name.value = ''; discover.value = false
}, { flush: 'sync' })
onUnmounted(() => { revision++; readSequence++ })
const pasted = computed(() => {
  if (!text.value.trim()) return { source: undefined, error: '' }
  try { return { source: identifyCookieSource('粘贴内容', text.value), error: '' } }
  catch (cause) { return { source: undefined, error: cause instanceof Error ? cause.message : '无法识别格式' } }
})
const sources = computed(() => tab.value === 'text' ? (pasted.value.source ? [pasted.value.source] : []) : files.value.flatMap(item => item.source ? [item.source] : []))
const remaining = computed(() => sources.value.filter(item => !results.value.some(result => result.ok && result.label === item.label)))
watch([text, tab], () => { results.value = []; error.value = '' })
const hasErrors = computed(() => tab.value === 'text' ? !!pasted.value.error : files.value.some(item => !!item.error))
async function readFiles(selected: File[]) {
  const sequence = ++readSequence, current = revision
  reading.value = true; error.value = ''; results.value = []
  const entries = await Promise.all(selected.map(async (file, index) => {
    const label = selected.filter(item => item.name === file.name).length > 1 ? `${file.name} (${index + 1})` : file.name
    try { return { name: label, size: file.size, source: identifyCookieSource(label, await file.text()) } }
    catch (cause) { return { name: label, size: file.size, error: cause instanceof Error ? cause.message : '文件读取失败，请重新选择' } }
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
async function submit() {
  if (store.importing || reading.value || hasErrors.value || !remaining.value.length) return
  const current = revision
  const imported = await store.importSources(remaining.value, { discover: discover.value, name: name.value.trim() })
  if (current !== revision || !props.modelValue) return
  results.value = [...results.value.filter(item => item.ok), ...imported]
  if (results.value.every(item => item.ok)) emit('update:modelValue', false)
  else error.value = '部分来源未导入，修正或重试失败项；成功项不会重复提交。'
}
const describe = (source: CookieSource) => `${source.format}${source.count ? ` · ${source.count} ${source.kind === 'bundle' ? '个账号条目' : '条 Cookie'}` : ''}`
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="导入账号"
    max-width="xl"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="space-y-5">
      <p class="text-sm text-muted">
        粘贴 Cookie，或选择凭据文件。自动识别格式，导入前不会联网识别账号。
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
          :disabled="store.importing"
          @click="tab = 'text'"
        >
          <FileText :size="16" />粘贴文本
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="tab === 'files'"
          :disabled="store.importing"
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
          placeholder="支持 Cookie 请求头、Cookie JSON 数组、storage state、Netscape cookies.txt 或账号包"
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
          <Upload :size="24" /><strong>{{ reading ? '正在读取文件…' : '选择或拖入文件' }}</strong><span>可同时选择多个 .json / .txt 文件</span>
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
          :key="file.name"
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
      <label class="import-discovery"><input
        v-model="discover"
        type="checkbox"
        :disabled="store.importing"
      ><div><strong>识别并导入 Cookie 中的多个登录身份</strong><small>开启后会访问 Google，识别 u/0、u/1 等身份；账号包按文件内条目导入。仅保存时，可在导入后从账号行识别邮箱和订阅。</small></div></label>
      <div
        v-if="results.length"
        class="space-y-2"
      >
        <p
          v-for="item in results"
          :key="item.label"
          class="text-sm"
          :class="item.ok ? 'text-success' : 'text-danger'"
        >
          <CheckCircle2
            v-if="item.ok"
            :size="14"
            class="inline"
          />{{ item.label }}：{{ item.ok ? `已导入 ${item.count} 个账号` : item.error }}
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
        @click="emit('update:modelValue', false)"
      >
        取消
      </Button><Button
        variant="primary"
        :loading="store.importing"
        :disabled="reading || hasErrors || !remaining.length"
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
</style>
