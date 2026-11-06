<script setup lang="ts">
import { ref, watch } from 'vue'
import { Activity, Save } from 'lucide-vue-next'
import { systemApi } from '@/api/system'
import { useToastStore } from '@/stores/toast'
import type { SystemConfig } from '@/types/system'
import Button from '@/components/ui/Button.vue'
import ConfigSelect from '@/components/config/ConfigSelect.vue'

const props = defineProps<{ initialConfig: SystemConfig }>()
const emit = defineEmits<{ saved: [] }>()
const toast = useToastStore()
const logLevel = ref(props.initialConfig.log_level || 'INFO')
const saving = ref(false)
watch(() => props.initialConfig.log_level, value => { logLevel.value = value || 'INFO' })
const logLevels = [
  { value: 'DEBUG', label: '调试 (DEBUG)' },
  { value: 'INFO', label: '信息 (INFO)' },
  { value: 'WARNING', label: '警告 (WARNING)' },
  { value: 'ERROR', label: '错误 (ERROR)' },
]
async function save() {
  saving.value = true
  try { await systemApi.updateLoggingConfig({ level: logLevel.value }); emit('saved'); toast.success('日志配置已保存') }
  catch (cause) { toast.error(cause instanceof Error ? cause.message : '保存失败') }
  finally { saving.value = false }
}
</script>

<template>
  <section class="bg-white border border-gray-200/80 rounded-2xl p-6 space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-4">
      <h3 class="flex items-center gap-2 font-semibold text-gray-900">
        <Activity :size="17" />日志配置
      </h3><Button
        :loading="saving"
        @click="save"
      >
        <Save :size="15" />保存日志配置
      </Button>
    </div>
    <ConfigSelect
      :model-value="logLevel"
      label="服务运行日志级别"
      :options="logLevels"
      @update:model-value="logLevel = String($event || 'INFO')"
    />
    <p class="text-sm text-gray-600 leading-relaxed">
      请求诊断自动记录脱敏元数据，在独立的日志与请求页面查看。记录保留 10 分钟，不保存对话内容或凭据。
    </p>
    <RouterLink
      to="/logs"
      class="text-sm text-brand-700 underline"
    >打开日志页面</RouterLink>
  </section>
</template>
