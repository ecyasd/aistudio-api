<script setup lang="ts">
import { ref, watch } from 'vue'
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

watch(
  () => props.account,
  (acc) => {
    if (acc) {
      inputName.value = acc.name || ''
    }
  },
  { immediate: true }
)

async function handleSave() {
  if (!props.account) return
  const trimmed = inputName.value.trim()
  if (!trimmed) return

  await accountsStore.updateAccountName(props.account.id, trimmed)
  emit('update:modelValue', false)
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
        <label class="block text-xs font-semibold text-secondary uppercase tracking-wider">
          账号名称 / 备注
        </label>
        <input
          v-model="inputName"
          type="text"
          placeholder="例如: 主账号 (Pro)、备用账号 1 等"
          class="w-full px-3.5 py-2.5 bg-canvas border border-line rounded-xl text-sm outline-none focus:bg-panel focus:border-accent-line focus:ring-2 focus:ring-accent/20 transition-all"
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
        :disabled="!inputName.trim()"
        @click="handleSave"
      >
        保存修改
      </Button>
    </template>
  </Modal>
</template>
