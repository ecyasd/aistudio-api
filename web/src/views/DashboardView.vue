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
  Brain,
  Database,
} from 'lucide-vue-next'

const accountsStore = useAccountsStore()
const systemStore = useSystemStore()

const activeAccountDisplay = computed(() => {
  const acc = accountsStore.activeAccount
  if (!acc) return '无激活账号'
  const sub = acc.auth_user != null && acc.auth_user !== '' ? ` (u/${acc.auth_user})` : ''
  return (acc.email || acc.name || acc.id) + sub
})

const activeAccountTierDisplay = computed(() => {
  const acc = accountsStore.activeAccount
  if (!acc) return undefined
  return `订阅: ${acc.tier || 'Free'}`
})

const totalAccountsCount = computed(() => accountsStore.accountRows.length)

const availableAccountsCount = computed(() => {
  const total = totalAccountsCount.value
  if (total === 0) return 0
  const available = accountsStore.accountRows.filter((a) => a.is_available !== false && a.enabled !== false).length
  return Math.min(available, total)
})

const reasoningTokensDisplay = computed(() => {
  const tokens = systemStore.stats?.totals?.reasoning_tokens || 0
  return tokens.toLocaleString()
})

const cachedTokensDisplay = computed(() => {
  const tokens = systemStore.stats?.totals?.cached_tokens || 0
  return tokens.toLocaleString()
})

async function loadData() {
  await Promise.all([
    accountsStore.fetchAll(),
    systemStore.fetchStats(),
  ])
}

usePolling(loadData, 10000)
</script>

<template>
  <div class="monitor-page space-y-6">
    <header class="monitor-heading">
      <div>
        <div class="eyebrow">
          WORKSPACE / OVERVIEW
        </div><h1>概览</h1><p>查看账号状态与 API 使用概况。</p>
      </div>
    </header>
    <!-- Stat Cards Grid -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
      <StatCard
        label="当前激活账号"
        :value="activeAccountDisplay"
        :sub-value="activeAccountTierDisplay"
        :color="accountsStore.activeAccount ? 'green' : 'default'"
      >
        <template #icon>
          <UserCheck class="w-4 h-4 text-success" />
        </template>
      </StatCard>

      <StatCard
        label="可用账号"
        :value="`${availableAccountsCount} / ${totalAccountsCount}`"
        color="default"
      >
        <template #icon>
          <Users class="w-4 h-4 text-accent" />
        </template>
      </StatCard>

      <StatCard
        label="总请求数"
        :value="systemStore.totalRequests"
        :sub-value="`成功 ${systemStore.totalSuccess}`"
        color="default"
      >
        <template #icon>
          <Activity class="w-4 h-4 text-info" />
        </template>
      </StatCard>

      <StatCard
        label="429 配额耗尽"
        :value="systemStore.totalRateLimited"
        :color="systemStore.totalRateLimited > 0 ? 'amber' : 'default'"
      >
        <template #icon>
          <AlertTriangle class="w-4 h-4 text-warning" />
        </template>
      </StatCard>

      <StatCard
        label="思考 Tokens"
        :value="reasoningTokensDisplay"
        color="default"
      >
        <template #icon>
          <Brain class="w-4 h-4 text-violet" />
        </template>
      </StatCard>

      <StatCard
        label="缓存 Tokens"
        :value="cachedTokensDisplay"
        color="default"
      >
        <template #icon>
          <Database class="w-4 h-4 text-success" />
        </template>
      </StatCard>
    </div>

    <!-- Model Statistics Table with Account Breakdown Accordion -->
    <ModelStatsTable
      :stats="systemStore.stats?.models || {}"
    />

    <!-- Quick API Reference Snippets -->
    <QuickApiExamples />
  </div>
</template>
