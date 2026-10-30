<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { diagnosticContext } from '@/utils/diagnosticContext.ts'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useSystemStore } from '@/stores/system.ts'
import { usePolling } from '@/composables/usePolling.ts'
import type { AccountWithStats } from '@/types/accounts.ts'
import Button from '@/components/ui/Button.vue'
import AccountTable from '@/components/accounts/AccountTable.vue'
import CookieImportModal from '@/components/modals/CookieImportModal.vue'
import EditAccountModal from '@/components/modals/EditAccountModal.vue'
import { Plus, RotateCcw, ArrowRightLeft } from 'lucide-vue-next'

const accountsStore = useAccountsStore()
const systemStore = useSystemStore()
const route = useRoute()
const focusedAccount = computed(() => diagnosticContext(route.query).account)

const importModalOpen = ref(false)
const editModalOpen = ref(false)
const editingAccount = ref<AccountWithStats | null>(null)

async function loadData() {
  await accountsStore.fetchAll()
}

// 页面活跃时每 8 秒自动轮询账号状态与配额
usePolling(loadData, 8000)

function handleEditName(acc: AccountWithStats) {
  editingAccount.value = acc
  editModalOpen.value = true
}

async function handleClearAllCooldowns() {
  if (confirm('确定要清除全部账号的所有模型 429 锁定吗？')) {
    await systemStore.clearCooldown()
    await accountsStore.fetchAll()
  }
}

async function handleForceNext() {
  await systemStore.forceNextAccount()
  await accountsStore.fetchAll()
}
</script>

<template>
  <div class="space-y-6">
    <!-- Top Action Bar -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl font-bold text-gray-900 tracking-tight">
          账号管理
        </h2>
      </div>

      <div class="flex items-center gap-2 flex-wrap">
        <Button
          variant="outline"
          size="md"
          :loading="systemStore.resettingCooldown"
          title="清除所有账号的 429 冷却与配额耗尽标记"
          @click="handleClearAllCooldowns"
        >
          <RotateCcw class="w-4 h-4" />
          <span>重置全部锁定</span>
        </Button>

        <Button
          variant="outline"
          size="md"
          :loading="systemStore.switchingNext"
          title="手动顺延切换至下一个健康账号"
          @click="handleForceNext"
        >
          <ArrowRightLeft class="w-4 h-4" />
          <span>切至下一账号</span>
        </Button>

        <Button
          variant="primary"
          size="md"
          @click="importModalOpen = true"
        >
          <Plus class="w-4 h-4" />
          <span>导入 Cookies</span>
        </Button>
      </div>
    </div>

    <div
      v-if="focusedAccount"
      class="rounded-xl border border-brand-100 bg-brand-50 px-4 py-3 text-xs text-gray-700 space-y-2"
    >
      <p class="font-mono break-all">
        诊断关联账号：{{ focusedAccount }}
      </p>
      <p v-if="!accountsStore.accountRows.some(account => account.id === focusedAccount)">
        此账号已删除或尚未载入，最近诊断仍按原账号 ID 保留至过期。
      </p>
      <div class="flex flex-wrap gap-4">
        <RouterLink
          to="/accounts"
          class="text-brand-700 underline underline-offset-4"
        >
          查看全部账号
        </RouterLink>
        <RouterLink
          :to="{ path: '/error-records', query: { account_id: focusedAccount } }"
          class="text-brand-700 underline underline-offset-4"
        >
          查看该账号最近诊断
        </RouterLink>
      </div>
    </div>

    <!-- Account Table -->
    <AccountTable
      :accounts="accountsStore.accountRows"
      :active-id="accountsStore.activeId"
      :focus-account="focusedAccount"
      @edit-name="handleEditName"
    />

    <!-- Cookie Import Modal -->
    <CookieImportModal v-model="importModalOpen" />

    <!-- Edit Account Name Modal -->
    <EditAccountModal
      v-model="editModalOpen"
      :account="editingAccount"
    />
  </div>
</template>
