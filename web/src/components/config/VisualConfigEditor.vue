<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import YAML from 'yaml'
import type { ParsedConfigYaml, ModelProfileItem, ModelOverrideMap } from './types.ts'
import ProfileCard from './ProfileCard.vue'
import ModelOverrideCard from './ModelOverrideCard.vue'
import ModelRulePreview from './ModelRulePreview.vue'
import SchemaDictionaryModal from './SchemaDictionaryModal.vue'
import ConfigSwitch from './ConfigSwitch.vue'
import FieldHelpTip from './FieldHelpTip.vue'
import Button from '@/components/ui/Button.vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import {
  FileCode,
  SlidersHorizontal,
  Save,
  Plus,
  RotateCcw,
  Layers,
  AlertCircle,
  BookOpen,
  Search,
} from 'lucide-vue-next'

const props = defineProps<{
  initialYaml?: string
}>()

const emit = defineEmits<{
  saved: []
}>()

const toast = useToastStore()

const activeTab = ref<'visual' | 'yaml'>('visual')
const rawYaml = ref('')
const parsedConfig = ref<ParsedConfigYaml>({})
const savedYaml = ref(serializeConfig({}))
const yamlParseError = ref<string | null>(null)
const saving = ref(false)
const dictModalOpen = ref(false)
const filterKeyword = ref('')
let pendingLoggingRefresh: { present: boolean; value: unknown } | undefined

const filteredProfilesWithIndex = computed(() => {
  const list = parsedConfig.value.model_defaults?.profiles || []
  const kw = filterKeyword.value.trim().toLowerCase()
  return list
    .map((profile, originalIndex) => ({ profile, originalIndex }))
    .filter(({ profile }) => {
      if (!kw) return true
      const inName = String(profile.name ?? '').toLowerCase().includes(kw)
      const inContains = profile.match?.contains?.some((c) => c.toLowerCase().includes(kw))
      const inPrefix = profile.match?.prefixes?.some((p) => p.toLowerCase().includes(kw))
      const inExact = profile.match?.exact?.some((e) => e.toLowerCase().includes(kw))
      return inName || inContains || inPrefix || inExact
    })
})

const modelOverrides = computed(() => parsedConfig.value.model_defaults?.models || {})
const previewYaml = computed(() => activeTab.value === 'yaml' ? rawYaml.value : serializeConfig(parsedConfig.value))

// API keys belong to the key manager. Never retain or submit a cached copy
// through the model editor, including when the user pastes an old config file.
function readEditorYaml(content: string): { yaml: string; config: ParsedConfigYaml } {
  const doc = YAML.parseDocument(content)
  if (doc.errors.length) throw new Error(doc.errors[0]?.message || 'YAML 解析失败')
  if (doc.contents !== null && !YAML.isMap(doc.contents)) {
    throw new Error('YAML 顶层必须是字典结构')
  }
  if (doc.contents !== null) doc.delete('api_keys')
  return { yaml: doc.toString(), config: (doc.toJS() || {}) as ParsedConfigYaml }
}

function serializeConfig(config: ParsedConfigYaml): string {
  return YAML.stringify(config, { indent: 2 })
}

function validateVisualShape(config: ParsedConfigYaml) {
  const isMap = (value: unknown): value is Record<string, unknown> =>
    value !== null && typeof value === 'object' && !Array.isArray(value)
  const fail = () => { throw new Error('模型规则结构不适用于可视化编辑，请在 YAML 中检查或编辑') }
  const checkRule = (rule: unknown) => {
    if (!isMap(rule)) return fail()
    if (rule.name != null && typeof rule.name !== 'string') fail()
    for (const key of ['default_tools', 'clear_generation_config_indexes']) {
      if (rule[key] != null && !Array.isArray(rule[key])) fail()
    }
    if (rule.match != null) {
      if (!isMap(rule.match)) return fail()
      for (const key of ['contains', 'prefixes', 'exact']) {
        const values = rule.match[key]
        if (values != null && (!Array.isArray(values) || values.some(v => typeof v !== 'string'))) fail()
      }
    }
    for (const key of ['generation_config_defaults', 'safety_settings']) {
      if (rule[key] != null && !isMap(rule[key])) fail()
    }
  }
  const defaults = config.model_defaults
  if (defaults == null) return
  if (!isMap(defaults)) return fail()
  if (defaults.profiles != null) {
    if (!Array.isArray(defaults.profiles)) return fail()
    defaults.profiles.forEach(checkRule)
  }
  if (defaults.models != null) {
    if (!isMap(defaults.models)) return fail()
    Object.values(defaults.models).forEach(checkRule)
  }
}

const hasUnsavedChanges = computed(() => {
  try {
    const config = activeTab.value === 'visual'
      ? parsedConfig.value
      : readEditorYaml(rawYaml.value).config
    return serializeConfig(config) !== savedYaml.value
  } catch {
    return true
  }
})

function parseYamlToState(content: string): boolean {
  try {
    const editor = readEditorYaml(content)
    rawYaml.value = editor.yaml
    validateVisualShape(editor.config)
    parsedConfig.value = editor.config
    yamlParseError.value = null
    return true
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : 'YAML 解析失败'
    yamlParseError.value = msg
    activeTab.value = 'yaml'
    return false
  }
}

function syncStateToYaml(): string {
  try {
    const dumped = serializeConfig(parsedConfig.value)
    rawYaml.value = dumped
    return dumped
  } catch (err) {
    console.error('序列化 YAML 失败:', err)
    return rawYaml.value
  }
}

function applyPendingLoggingRefresh() {
  const refresh = pendingLoggingRefresh
  if (!refresh) return
  try {
    const baseline = readEditorYaml(savedYaml.value).config
    const draft = activeTab.value === 'visual'
      ? parsedConfig.value
      : readEditorYaml(rawYaml.value).config
    // A model draft must not restore logging saved by the separate card.
    // Explicit edits to logging in YAML remain the owner's local draft.
    if (serializeConfig({ logging: draft.logging }) === serializeConfig({ logging: baseline.logging })) {
      if (activeTab.value === 'yaml') {
        const doc = YAML.parseDocument(readEditorYaml(rawYaml.value).yaml)
        if (refresh.present) doc.set('logging', refresh.value)
        else doc.delete('logging')
        if (!parseYamlToState(doc.toString())) return
      } else {
        const updated = { ...draft }
        if (refresh.present) updated.logging = refresh.value
        else delete updated.logging
        parsedConfig.value = updated
      }
      if (refresh.present) baseline.logging = refresh.value
      else delete baseline.logging
      savedYaml.value = serializeConfig(baseline)
    }
    // Explicit logging edits keep their original baseline. Advancing it here
    // would hide a same-field conflict with the separately saved logging card.
    pendingLoggingRefresh = undefined
  } catch {
    // Retain the refresh until the owner repairs the invalid YAML draft.
  }
}

watch(
  () => props.initialYaml,
  (val) => {
    // Refreshes from the other settings cards must not discard a model draft.
    if (val === undefined) return
    if (hasUnsavedChanges.value) {
      try {
        const incoming = readEditorYaml(val).config
        pendingLoggingRefresh = { present: 'logging' in incoming, value: incoming.logging }
        applyPendingLoggingRefresh()
      } catch {
        // Invalid YAML is still an unsaved draft; leave its text untouched.
      }
      return
    }
    if (parseYamlToState(val)) {
      savedYaml.value = serializeConfig(parsedConfig.value)
      pendingLoggingRefresh = undefined
    } else {
      // Keep a malformed loaded draft editable, including valid legacy rules
      // that the visual form cannot represent. Never replace it with {}.
      try {
        const editor = readEditorYaml(val)
        rawYaml.value = editor.yaml
        savedYaml.value = serializeConfig(editor.config)
      } catch {
        rawYaml.value = val
      }
    }
  },
  { immediate: true }
)

function switchTab(tab: 'visual' | 'yaml') {
  if (tab === activeTab.value) return
  if (tab === 'yaml') {
    syncStateToYaml()
  } else {
    const ok = parseYamlToState(rawYaml.value)
    if (!ok) {
      toast.error('当前 YAML 语法有误，无法切换至可视化视图: ' + yamlParseError.value)
      return
    }
  }
  activeTab.value = tab
  applyPendingLoggingRefresh()
}

function handleAddProfile() {
  const currentProfiles = parsedConfig.value.model_defaults?.profiles || []
  const newProfile: ModelProfileItem = {
    name: `profile_${currentProfiles.length + 1}`,
    match: {
      prefixes: ['gemini-'],
    },
    default_tools: ['google_search'],
    safety_settings: {
      Harassment: 5,
      Hate: 5,
      'Sexually Explicit': 5,
      'Dangerous Content': 5,
    },
    drop_unsupported_params: true,
  }
  const nextModelDefaults = {
    ...(parsedConfig.value.model_defaults || {}),
    profiles: [...currentProfiles, newProfile],
  }
  parsedConfig.value = {
    ...parsedConfig.value,
    model_defaults: nextModelDefaults,
  }
}

function updateProfile(index: number, updated: ModelProfileItem) {
  const currentProfiles = [...(parsedConfig.value.model_defaults?.profiles || [])]
  currentProfiles[index] = updated
  const nextModelDefaults = {
    ...(parsedConfig.value.model_defaults || {}),
    profiles: currentProfiles,
  }
  parsedConfig.value = {
    ...parsedConfig.value,
    model_defaults: nextModelDefaults,
  }
}

function deleteProfile(index: number) {
  const currentProfiles = [...(parsedConfig.value.model_defaults?.profiles || [])]
  currentProfiles.splice(index, 1)
  const nextModelDefaults = {
    ...(parsedConfig.value.model_defaults || {}),
    profiles: currentProfiles,
  }
  parsedConfig.value = {
    ...parsedConfig.value,
    model_defaults: nextModelDefaults,
  }
}

function updateModelsOverride(models: ModelOverrideMap) {
  const nextModelDefaults = {
    ...(parsedConfig.value.model_defaults || {}),
    models,
  }
  parsedConfig.value = {
    ...parsedConfig.value,
    model_defaults: nextModelDefaults,
  }
}

function updateGlobalDropUnsupported(val: boolean) {
  const nextModelDefaults = {
    ...(parsedConfig.value.model_defaults || {}),
    drop_unsupported_params: val,
  }
  parsedConfig.value = {
    ...parsedConfig.value,
    model_defaults: nextModelDefaults,
  }
}

function handleResetDefaults() {
  if (confirm('确定要将所有模型规则重置为官方推荐默认预设吗？')) {
    const currentLogging = (parsedConfig.value as Record<string, unknown>).logging || {
      level: 'INFO',
      dump_requests: false,
    }
    const doc: ParsedConfigYaml = {
      logging: currentLogging,
      model_defaults: {
        drop_unsupported_params: false,
        profiles: [
          {
            name: 'image_models',
            match: {
              contains: ['image'],
            },
            is_image_model: true,
            default_tools: ['google_search_and_image_search'],
            generation_config_defaults: {
              response_mime_type: null,
              image_output_mode: 'image_only',
              thinking_config: {
                level: 'MINIMAL',
                mode: 1,
              },
            },
            clear_generation_config_indexes: [7, 13, 17],
            disable_safety_settings: true,
          },
          {
            name: 'gemma_models',
            match: {
              prefixes: ['gemma-'],
            },
            default_tools: ['google_search'],
            safety_settings: {
              Harassment: 5,
              Hate: 5,
              'Sexually Explicit': 5,
              'Dangerous Content': 5,
            },
          },
          {
            name: 'gemini_models',
            match: {
              prefixes: ['gemini-'],
            },
            default_tools: ['google_search'],
            safety_settings: {
              Harassment: 5,
              Hate: 5,
              'Sexually Explicit': 5,
              'Dangerous Content': 5,
            },
            drop_unsupported_params: true,
          },
        ],
        models: {},
      },
    }
    parsedConfig.value = doc
    syncStateToYaml()
    toast.info('已载入默认规则（API Key 由上方独立管理，日志配置保留），请点击右上角保存并生效')
  }
}

async function handleSave() {
  if (saving.value) return
  applyPendingLoggingRefresh()
  saving.value = true
  let contentToSave: string
  if (activeTab.value === 'visual') {
    contentToSave = syncStateToYaml()
  } else {
    try {
      contentToSave = readEditorYaml(rawYaml.value).yaml
      rawYaml.value = contentToSave
    } catch (err) {
      toast.error('YAML 格式错误: ' + (err instanceof Error ? err.message : '解析失败'))
      saving.value = false
      return
    }
  }

  try {
    await systemApi.updateConfigYaml(contentToSave, savedYaml.value)
    savedYaml.value = serializeConfig(readEditorYaml(contentToSave).config)
    toast.success('模型规则配置已成功保存并完成热重载！')
    emit('saved')
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '保存配置失败'
    toast.error(msg)
  } finally {
    saving.value = false
  }
}
function handleYamlKeydown(e: KeyboardEvent) {
  if (e.key === ']' && (e.ctrlKey || e.metaKey) && !e.altKey) {
    e.preventDefault()
    const target = e.target as HTMLTextAreaElement
    const start = target.selectionStart
    const end = target.selectionEnd
    const val = rawYaml.value
    rawYaml.value = val.substring(0, start) + '  ' + val.substring(end)
    requestAnimationFrame(() => {
      target.selectionStart = target.selectionEnd = start + 2
    })
  }
}

onBeforeRouteLeave((_to, _from, next) => {
  if (hasUnsavedChanges.value) {
    if (confirm('当前配置有未保存的修改，离开本页将丢失修改，确定离开吗？')) {
      next()
    } else {
      next(false)
    }
  } else {
    next()
  }
})
</script>

<template>
  <div class="bg-white border border-gray-200/80 rounded-2xl p-6 shadow-xs space-y-5">
    <!-- Header & Mode Switcher -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-gray-100 pb-5">
      <div>
        <div class="flex items-center gap-2">
          <SlidersHorizontal class="w-4 h-4 text-brand-600" />
          <h3 class="font-bold text-gray-900 text-sm">
            模型规则与安全策略 (config.yaml)
          </h3>
        </div>
        <p class="text-xs text-gray-500 mt-2">
          API 密钥由上方密钥管理独立保存，导入或激活账号、保存模型规则均不会更换调用密钥。
        </p>
      </div>

      <!-- Controls & Actions -->
      <div class="flex items-center gap-2 flex-wrap">
        <Button
          variant="secondary"
          size="sm"
          title="查看所有配置项详细规范与推荐说明"
          @click="dictModalOpen = true"
        >
          <BookOpen class="w-3.5 h-3.5 text-brand-600" />
          <span>参数规范字典</span>
        </Button>

        <!-- Tab Pill Toggle -->
        <div class="flex items-center bg-gray-100 p-1 rounded-xl text-xs font-medium">
          <button
            type="button"
            class="px-3 py-1.5 rounded-lg transition-all flex items-center gap-1.5 cursor-pointer"
            :class="activeTab === 'visual' ? 'bg-white text-brand-700 shadow-2xs font-semibold' : 'text-gray-600 hover:text-gray-900'"
            @click="switchTab('visual')"
          >
            <SlidersHorizontal class="w-3.5 h-3.5" />
            <span>可视化模式</span>
          </button>
          <button
            type="button"
            class="px-3 py-1.5 rounded-lg transition-all flex items-center gap-1.5 cursor-pointer"
            :class="activeTab === 'yaml' ? 'bg-white text-brand-700 shadow-2xs font-semibold' : 'text-gray-600 hover:text-gray-900'"
            @click="switchTab('yaml')"
          >
            <FileCode class="w-3.5 h-3.5" />
            <span>YAML 源码</span>
          </button>
        </div>

        <Button
          variant="ghost"
          size="sm"
          title="重置为官方推荐规则"
          @click="handleResetDefaults"
        >
          <RotateCcw class="w-3.5 h-3.5" />
          <span class="hidden md:inline">重置默认</span>
        </Button>

        <Button
          variant="primary"
          size="sm"
          :loading="saving"
          @click="handleSave"
        >
          <Save class="w-3.5 h-3.5" />
          <span>保存并热重载</span>
        </Button>
      </div>
    </div>

    <ModelRulePreview :yaml-content="previewYaml" />

    <!-- Search Filter Bar in Visual Mode -->
    <div
      v-if="activeTab === 'visual'"
      class="relative"
    >
      <Search class="w-4 h-4 text-gray-400 absolute left-3.5 top-1/2 -translate-y-1/2 pointer-events-none" />
      <input
        v-model="filterKeyword"
        type="text"
        placeholder="在下方快速筛选规则组或模型名称（如：image、gemini、gemma）..."
        class="w-full pl-10 pr-4 py-2 bg-gray-50 border border-gray-200 rounded-xl text-xs outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 font-mono transition-all"
      >
    </div>

    <!-- Error Banner if YAML is invalid -->
    <div
      v-if="yamlParseError"
      class="p-3.5 bg-rose-50 border border-rose-200 rounded-xl text-xs text-rose-800 flex items-start gap-2.5"
    >
      <AlertCircle class="w-4 h-4 text-rose-600 shrink-0 mt-0.5" />
      <div>
        <div class="font-semibold">
          YAML 语法解析错误
        </div>
        <div class="mt-0.5 font-mono text-[11px]">
          {{ yamlParseError }}
        </div>
      </div>
    </div>

    <!-- Mode 1: Visual Form Editor -->
    <div
      v-if="activeTab === 'visual'"
      class="space-y-6"
    >
      <!-- Global Drop Unsupported Params Switch -->
      <div class="p-4 bg-gray-50/80 border border-gray-200/80 rounded-2xl flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <div class="text-xs font-bold text-gray-900 flex items-center gap-1.5">
            <span>全局丢弃不支持参数 (drop_unsupported_params)</span>
            <span class="text-[10px] text-brand-700 bg-brand-50 px-1.5 py-0.5 rounded font-mono border border-brand-200/60 font-semibold">全局默认</span>
            <FieldHelpTip schema-key="system.drop_unsupported_params" />
          </div>
          <p class="text-[11px] text-gray-400 mt-0.5">
            自动过滤下游客户端传入的未知安全类别（如 HARM_CATEGORY_CIVIC_INTEGRITY）或模型不支持的工具，避免 400 报错阻断
          </p>
        </div>
        <div class="shrink-0">
          <ConfigSwitch
            :model-value="Boolean(parsedConfig.model_defaults?.drop_unsupported_params)"
            label="启用全局自动丢弃"
            @update:model-value="updateGlobalDropUnsupported($event)"
          />
        </div>
      </div>

      <!-- Section 1: Model Profiles -->
      <div class="space-y-4">
        <div class="flex items-center justify-between border-b border-gray-100 pb-3">
          <div>
            <h4 class="text-xs font-bold text-gray-900 flex items-center gap-2">
              <Layers class="w-4 h-4 text-brand-600" />
              <span>模型分组规则 (Model Profiles)</span>
            </h4>
          </div>

          <Button
            variant="secondary"
            size="sm"
            @click="handleAddProfile"
          >
            <Plus class="w-3.5 h-3.5" />
            <span>添加规则组</span>
          </Button>
        </div>

        <div
          v-if="filteredProfilesWithIndex.length"
          class="space-y-4"
        >
          <ProfileCard
            v-for="item in filteredProfilesWithIndex"
            :key="item.originalIndex"
            :profile="item.profile"
            :index="item.originalIndex"
            @update:profile="updateProfile(item.originalIndex, $event)"
            @delete="deleteProfile(item.originalIndex)"
          />
        </div>
        <div
          v-else-if="filterKeyword.trim()"
          class="p-6 text-center text-gray-400 text-xs border border-dashed border-gray-200 rounded-2xl"
        >
          未找到匹配 "{{ filterKeyword }}" 的模型分组规则
        </div>
        <div
          v-else
          class="p-8 text-center text-gray-400 text-xs border border-dashed border-gray-200 rounded-2xl"
        >
          未配置任何模型分组规则。点击上方按钮添加规则。
        </div>
      </div>

      <!-- Section 2: Specific Model Overrides -->
      <ModelOverrideCard
        :models="modelOverrides"
        :filter-keyword="filterKeyword"
        @update:models="updateModelsOverride"
      />
    </div>

    <!-- Mode 2: Raw YAML Code Editor -->
    <div
      v-else
      class="space-y-2"
    >
      <div class="flex items-center justify-between text-xs text-gray-400 font-mono">
        <span>config.yaml 直接编辑</span>
        <span>保存后自动热重载，无需重启进程</span>
      </div>
      <textarea
        v-model="rawYaml"
        aria-label="模型规则 YAML"
        rows="22"
        spellcheck="false"
        placeholder="正在加载 config.yaml..."
        class="w-full p-4 font-mono text-xs bg-gray-900 text-gray-100 rounded-xl outline-none focus:ring-2 focus:ring-brand-500 leading-relaxed shadow-inner border border-gray-800"
        @keydown="handleYamlKeydown"
      />
      <p class="text-xs text-gray-500">
        Tab / Shift+Tab 切换焦点；Ctrl+]（Mac 为 Command+]）插入两个空格。
      </p>
    </div>

    <!-- Schema Specification Dictionary Modal -->
    <SchemaDictionaryModal v-model="dictModalOpen" />
  </div>
</template>
