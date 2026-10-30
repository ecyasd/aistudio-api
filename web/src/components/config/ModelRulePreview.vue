<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { FlaskConical } from 'lucide-vue-next'
import { systemApi } from '@/api/system.ts'
import type { ModelRulePreview } from '@/types/system.ts'
import Button from '@/components/ui/Button.vue'

const props = defineProps<{ yamlContent: string }>()
const model = ref('')
const preview = ref<ModelRulePreview | null>(null)
const loading = ref(false)
const error = ref('')
let sequence = 0
let controller: AbortController | null = null

function invalidate() {
  ++sequence
  controller?.abort()
  controller = null
  loading.value = false
  preview.value = null
  error.value = ''
}
watch([model, () => props.yamlContent], invalidate, { flush: 'sync' })
onUnmounted(invalidate)

async function runPreview() {
  invalidate()
  if (!model.value.trim()) return
  const current = sequence
  controller = new AbortController()
  loading.value = true
  try {
    const result = await systemApi.previewModelRules(model.value.trim(), props.yamlContent, controller.signal)
    if (current === sequence) preview.value = result
  } catch (err) {
    if (current === sequence) error.value = err instanceof Error ? err.message : '预览失败，请重试。'
  } finally {
    if (current === sequence) { loading.value = false; controller = null }
  }
}

const generationLabels: Record<string, string> = {
  temperature: '温度', top_p: 'Top P', top_k: 'Top K', max_tokens: '最大输出 token',
  response_mime_type: '响应格式', response_schema: '响应结构', stop_sequences: '停止序列',
  thinking_config: '思考配置', image_output_mode: '图片输出', media_resolution: '媒体分辨率',
}
function formatValue(key: string, value: unknown): string {
  if (value === null) return '清空此默认值'
  if (key === 'thinking_config' && Array.isArray(value)) {
    const levels: Record<number, string> = { 1: 'LOW', 2: 'MEDIUM', 3: 'HIGH', 4: 'MINIMAL' }
    return `级别 ${levels[Number(value[3])] ?? String(value[3])} · 模式 ${String(value[0])}`
  }
  if (key === 'image_output_mode' && Array.isArray(value)) return value[1] === 1 ? '文本与图片' : '仅图片'
  return typeof value === 'string' ? value : JSON.stringify(value)
}
const fields = computed(() => {
  const value = preview.value?.effective
  if (!value) return []
  return [
    ['模型类型', value.is_image_model ? '图片模型' : '文本 / 通用模型'],
    ['默认工具', value.default_tools.join('、') || '无'],
    ['不支持的请求参数', value.drop_unsupported_params ? '丢弃' : '报错提醒'],
    ['安全设置', value.is_image_model && value.disable_safety_settings ? '不发送安全设置（图片模型）' : value.safety_settings === null ? '沿用请求 / 上游模板' : '使用下方配置，可由客户端显式参数覆盖'],
    ...Object.entries(value.generation_config_defaults).map(([key, val]) => [generationLabels[key] ?? key, formatValue(key, val)]),
  ]
})
</script>

<template>
  <section
    class="rounded-xl border border-brand-100 bg-brand-50/30 p-4 space-y-4"
    aria-labelledby="model-preview-title"
  >
    <div>
      <h4
        id="model-preview-title"
        class="flex items-center gap-2 text-sm font-semibold text-gray-900"
      >
        <FlaskConical class="w-4 h-4 text-brand-600" />模型规则生效预览
      </h4>
      <p
        id="model-preview-hint"
        class="mt-1.5 text-xs text-gray-600 leading-relaxed"
      >
        使用当前草稿计算，无需先保存，也不会调用模型。输入酒馆中选择的模型名，支持“抗截断/”前缀。
      </p>
    </div>
    <form
      class="flex flex-col sm:flex-row sm:items-end gap-3"
      :aria-busy="loading"
      @submit.prevent="runPreview"
    >
      <label class="flex-1 text-xs font-medium text-gray-700">
        模型名称
        <input
          v-model="model"
          maxlength="160"
          placeholder="例如 gemini-2.5-pro"
          autocomplete="off"
          spellcheck="false"
          :aria-describedby="error ? 'model-preview-hint model-preview-error' : 'model-preview-hint'"
          class="mt-1.5 block w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm font-mono outline-none focus:ring-2 focus:ring-brand-300"
        >
      </label>
      <Button
        type="submit"
        variant="secondary"
        :loading="loading"
        :disabled="!model.trim()"
      >
        计算当前草稿
      </Button>
    </form>
    <p
      v-if="error"
      id="model-preview-error"
      role="alert"
      class="text-sm text-rose-700"
    >
      {{ error }}
    </p>
    <p
      v-if="loading"
      role="status"
      class="text-xs text-gray-600"
    >
      正在计算当前草稿…
    </p>
    <div
      v-if="preview"
      class="space-y-3"
      aria-live="polite"
    >
      <p class="text-xs text-gray-600 break-all">
        实际模型：<span class="font-mono text-gray-900">{{ preview.model }}</span>
        · {{ preview.auto_continue ? '抗截断模式' : '普通模式' }} · 当前草稿
      </p>
      <ol
        v-if="preview.matched_rules.length"
        class="flex flex-wrap gap-2 text-xs"
        aria-label="规则命中顺序"
      >
        <li
          v-for="(rule, index) in preview.matched_rules"
          :key="`${rule.kind}-${rule.index}-${rule.name}`"
          class="rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 break-all"
        >
          {{ index + 1 }}. {{ rule.kind === 'model' ? '精确覆盖' : `规则组 #${(rule.index ?? 0) + 1}` }}：{{ rule.name }}
        </li>
      </ol>
      <p
        v-else
        class="text-xs text-gray-600"
      >
        没有命中规则组或精确覆盖，沿用全局基础配置。
      </p>
      <dl class="grid sm:grid-cols-2 gap-3 rounded-xl border border-gray-200 bg-white p-3 text-xs">
        <div
          v-for="[label, value] in fields"
          :key="label"
          class="min-w-0"
        >
          <dt class="text-gray-500">
            {{ label }}
          </dt><dd class="mt-1 text-gray-900 break-all whitespace-pre-wrap">
            {{ value }}
          </dd>
        </div>
      </dl>
      <details
        v-if="preview.effective.safety_settings && !(preview.effective.is_image_model && preview.effective.disable_safety_settings)"
        class="text-xs text-gray-600"
      >
        <summary class="cursor-pointer">
          安全分类配置
        </summary>
        <p class="mt-2">
          {{ preview.effective.safety_settings.map(item => `${({ 7: '骚扰', 8: '仇恨', 9: '色情', 10: '危险内容' } as Record<number, string>)[Number(item[2])] ?? item[2]}：${item[3]}`).join(' · ') }}
        </p>
      </details>
      <p
        v-if="preview.effective.clear_generation_config_indexes.length"
        class="text-xs text-amber-800"
      >
        {{ preview.effective.is_image_model ? '图片规则最后会清除部分生成参数' : '已配置清除位置，但仅图片模型会应用' }}（位置 {{ preview.effective.clear_generation_config_indexes.join('、') }}），请结合参数字典检查。
      </p>
      <p
        v-if="preview.effective.disable_safety_settings && !preview.effective.is_image_model"
        class="text-xs text-amber-800"
      >
        disable_safety_settings 仅对图片模型生效，本模型沿用上方安全配置。
      </p>
      <p
        v-if="preview.ignored_generation_keys.length"
        class="text-xs text-amber-800"
      >
        以下生成参数未被编码器识别，不会生效：{{ preview.ignored_generation_keys.join('、') }}
      </p>
      <p class="text-xs text-gray-500 leading-relaxed">
        多个规则组按顺序合并工具与清除位置；精确模型规则最后覆盖。这里展示默认规则；酒馆显式发送的温度、输出上限等参数还会覆盖对应默认值。未列出的生成参数由请求或上游模板决定。
      </p>
    </div>
  </section>
</template>
