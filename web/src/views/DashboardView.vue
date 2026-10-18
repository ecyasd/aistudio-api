<script setup lang="ts">
import { computed } from 'vue'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useSystemStore } from '@/stores/system.ts'
import { usePolling } from '@/composables/usePolling.ts'
import StatCard from '@/components/ui/StatCard.vue'
import ModelStatsTable from '@/components/dashboard/ModelStatsTable.vue'
import QuickApiExamples from '@/components/dashboard/QuickApiExamples.vue'
import {
  UserCheck,
  Users,
  Activity,
  AlertTriangle,
  ArrowRight,
  FileWarning,
} from 'lucide-vue-next'

const accountsStore = useAccountsStore()
const systemStore = useSystemStore()

const activeAccountDisplay = computed(() => {
  const acc = accountsStore.activeAccount
  if (!acc) return '无激活账号'
  const sub = acc.auth_user != null && acc.auth_user !== '' ? ` (u/${acc.auth_user})` : ''
  return (acc.email || acc.name || acc.id) + sub
})

const totalAccountsCount = computed(() => accountsStore.accountRows.length)

const availableAccountsCount = computed(() => {
  const total = totalAccountsCount.value
  if (total === 0) return 0
  const available = accountsStore.accountRows.filter((a) => a.is_available !== false).length
  return Math.min(available, total)
})

async function loadData() {
  await Promise.all([
    accountsStore.fetchAll(),
    systemStore.fetchStats(),
  ])
}

// 页面可见时每 10 秒自动轮询一次统计与账号状态
usePolling(loadData, 10000)
</script>

<template>
  <div class="space-y-6">
    <!-- Stat Cards Grid -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <StatCard
        label="当前激活账号"
        :value="activeAccountDisplay"
        :color="accountsStore.activeAccount ? 'green' : 'default'"
      >
        <template #icon>
          <UserCheck class="w-4 h-4 text-emerald-600" />
        </template>
      </StatCard>

      <StatCard
        label="可用账号"
        :value="`${availableAccountsCount} / ${totalAccountsCount}`"
        color="default"
      >
        <template #icon>
          <Users class="w-4 h-4 text-brand-600" />
        </template>
      </StatCard>

      <StatCard
        label="总请求数"
        :value="systemStore.totalRequests"
        :sub-value="`成功 ${systemStore.totalSuccess}`"
        color="default"
      >
        <template #icon>
          <Activity class="w-4 h-4 text-blue-600" />
        </template>
      </StatCard>

      <StatCard
        label="429 配额耗尽次数"
        :value="systemStore.totalRateLimited"
        :color="systemStore.totalRateLimited > 0 ? 'amber' : 'default'"
      >
        <template #icon>
          <AlertTriangle class="w-4 h-4 text-amber-500" />
        </template>
      </StatCard>
    </div>

    <RouterLink
      to="/error-records"
      class="flex items-center justify-between gap-4 p-4 sm:p-5 rounded-2xl border border-gray-200 bg-white hover:border-brand-300 hover:bg-brand-50/30 transition-colors"
    >
      <div class="flex items-center gap-3">
        <FileWarning class="w-5 h-5 text-rose-500 shrink-0" />
        <div>
          <p class="text-sm font-semibold text-gray-900">
            查看实时错误
          </p>
          <p class="text-xs text-gray-500 mt-1">
            查看最近 10 分钟的 API 失败、流式中断、浏览器异常和自动重试
          </p>
        </div>
      </div>
      <ArrowRight class="w-4 h-4 text-gray-400 shrink-0" />
    </RouterLink>

    <!-- Model Statistics Table -->
    <ModelStatsTable
      :stats="systemStore.stats?.models || {}"
    />

    <!-- Quick API Reference Snippets -->
    <QuickApiExamples />
  </div>
</template>
