<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import type { AccountWithStats } from '@/types'
import { useAccountsStore } from '@/stores/accounts.ts'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'

const props = defineProps<{
  modelValue: boolean
  account: AccountWithStats | null
}>()

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

const accountsStore = useAccountsStore()
const inputName = ref('')
const saving = ref(false)
let dialogRevision = 0
let mounted = true

watch(
  [() => props.modelValue, () => props.account?.id],
  () => {
    dialogRevision++
    if (props.modelValue && props.account) {
      inputName.value = props.account.name || ''
    }
  },
  { immediate: true, flush: 'sync' }
)

onUnmounted(() => {
  mounted = false
  dialogRevision++
})

async function handleSave() {
  if (!props.modelValue || !props.account || saving.value) return
  const trimmed = inputName.value.trim()
  if (!trimmed) return
  const id = props.account.id
  const revision = dialogRevision
  saving.value = true
  try {
    const ok = await accountsStore.updateAccountName(id, trimmed)
    if (ok && mounted && props.modelValue && dialogRevision === revision &&
        props.account?.id === id && inputName.value.trim() === trimmed) {
      emit('update:modelValue', false)
    }
  } finally {
    if (mounted) saving.value = false
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="修改账号名称"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="space-y-4">
      <div class="space-y-1.5">
        <label class="block text-xs font-semibold text-gray-700 uppercase tracking-wider">
          账号名称 / 备注
        </label>
        <input
          v-model="inputName"
          type="text"
          placeholder="例如: 主账号 (Pro)、备用账号 1 等"
          class="w-full px-3.5 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
        >
      </div>
    </div>

    <template #footer>
      <Button
        variant="ghost"
        size="sm"
        @click="emit('update:modelValue', false)"
      >
        取消
      </Button>
      <Button
        variant="primary"
        size="sm"
        :loading="saving"
        :disabled="!inputName.trim()"
        @click="handleSave"
      >
        保存修改
      </Button>
    </template>
  </Modal>
</template>
