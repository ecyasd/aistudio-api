<script setup lang="ts">
import { ref, computed } from 'vue'
import { Menu } from 'lucide-vue-next'
import { useRoute } from 'vue-router'
import Sidebar from './Sidebar.vue'
import TokenSettingsModal from '@/components/modals/TokenSettingsModal.vue'
import ToastContainer from '@/components/ui/ToastContainer.vue'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useSystemStore } from '@/stores/system.ts'
import { useToastStore } from '@/stores/toast.ts'

const route = useRoute()
const accountsStore = useAccountsStore()
const systemStore = useSystemStore()
const toast = useToastStore()

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
  <div class="console-shell flex">
    <ToastContainer />

    <!-- Sidebar -->
    <Sidebar
      :open="sidebarOpen"
      @update:open="sidebarOpen = $event"
      @open-token-modal="tokenModalOpen = true"
    />

    <!-- Main Content Area -->
    <div class="console-main flex flex-col">
      <header class="console-topbar">
        <button
          type="button"
          class="console-button console-mobile-menu"
          aria-label="打开导航"
          :aria-expanded="sidebarOpen"
          @click="sidebarOpen = !sidebarOpen"
        >
          <Menu :size="18" />
        </button>
        <span>控制台 / {{ currentTitle }}</span>
        <button
          v-if="!['requests', 'logs'].includes(String(route.name))"
          type="button"
          class="console-button"
          :disabled="refreshing"
          @click="handleRefresh"
        >
          刷新
        </button>
        <span>SILLYTAVERN · API CONSOLE</span>
      </header>
      <main class="console-content w-full flex-1">
        <slot />
      </main>
    </div>

    <!-- Global Token Settings Modal -->
    <TokenSettingsModal v-model="tokenModalOpen" />
  </div>
</template>
