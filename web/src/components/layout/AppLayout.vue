<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import Sidebar from './Sidebar.vue'
import Topbar from './Topbar.vue'
import TokenSettingsModal from '@/components/modals/TokenSettingsModal.vue'
import ToastContainer from '@/components/ui/ToastContainer.vue'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useSystemStore } from '@/stores/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import { useErrorsStore } from '@/stores/errors.ts'

const route = useRoute()
const accountsStore = useAccountsStore()
const systemStore = useSystemStore()
const toast = useToastStore()
const errorsStore = useErrorsStore()

const sidebarOpen = ref(false)
const tokenModalOpen = ref(false)
const refreshing = ref(false)

const currentTitle = computed(() => {
  const t = route.meta.title
  return typeof t === 'string' ? t.replace(' - AI Studio Proxy', '') : 'AI Studio Proxy'
})

async function handleRefresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    if (route.name === 'error-records') {
      if (await errorsStore.refresh()) toast.success('错误记录已刷新')
      else toast.error('错误记录读取失败，请查看页面提示')
      return
    }
    await Promise.allSettled([
      accountsStore.fetchAll(),
      systemStore.fetchStats(),
      systemStore.fetchRotation(),
      systemStore.checkHealth(),
    ])
    toast.success('数据已刷新')
  } finally {
    refreshing.value = false
  }
}
</script>

<template>
  <div class="min-h-screen bg-gray-50 flex">
    <ToastContainer />

    <!-- Sidebar -->
    <Sidebar
      :open="sidebarOpen"
      @update:open="sidebarOpen = $event"
      @open-token-modal="tokenModalOpen = true"
    />

    <!-- Main Content Area -->
    <div class="flex-1 lg:pl-64 flex flex-col min-w-0">
      <Topbar
        :title="currentTitle"
        :refreshing="refreshing"
        @toggle-sidebar="sidebarOpen = !sidebarOpen"
        @open-token-modal="tokenModalOpen = true"
        @refresh="handleRefresh"
      />

      <main class="flex-1 p-4 sm:p-6 lg:p-8 max-w-7xl w-full mx-auto">
        <slot />
      </main>
    </div>

    <!-- Global Token Settings Modal -->
    <TokenSettingsModal v-model="tokenModalOpen" />
  </div>
</template>
