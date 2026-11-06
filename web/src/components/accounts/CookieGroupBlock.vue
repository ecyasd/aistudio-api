<script setup lang="ts">
import type { CookieGroup } from './useCookieGroups.ts'
import { formatDate } from '@/utils/format.ts'
import {
  Cookie,
  ChevronDown,
  ChevronRight,
  FolderMinus,
} from 'lucide-vue-next'

defineProps<{
  group: CookieGroup
  collapsed: boolean
}>()

const emit = defineEmits<{
  toggle: []
  deleteGroup: []
}>()
</script>

<template>
  <div class="transition-colors">
    <!-- Level 1: Cookie Credential Parent Node -->
    <div
      class="px-5 py-3.5 flex items-center justify-between gap-3 cursor-pointer select-none transition-colors"
      :class="[
        group.hasActive ? 'bg-accent-soft/30 hover:bg-accent-soft/50' : 'bg-panel hover:bg-canvas/80',
      ]"
      @click="emit('toggle')"
    >
      <div class="flex items-center gap-3 min-w-0">
        <!-- Expand Chevron -->
        <button
          type="button"
          class="p-1 rounded-md text-muted hover:text-secondary hover:bg-line/60 transition-colors"
        >
          <ChevronDown
            v-if="!collapsed"
            class="w-4 h-4"
          />
          <ChevronRight
            v-else
            class="w-4 h-4"
          />
        </button>

        <!-- Cookie Icon -->
        <div
          class="w-7 h-7 rounded-lg flex items-center justify-center shrink-0 "
          :class="group.hasActive ? 'bg-accent text-on-accent' : 'bg-raised text-secondary'"
        >
          <Cookie class="w-4 h-4" />
        </div>

        <!-- Group Info -->
        <div class="truncate">
          <div class="flex items-center gap-2">
            <span class="font-semibold text-fg text-xs tracking-tight">
              {{ group.name }}
            </span>
            <span class="text-[11px] font-mono text-muted">
              ({{ group.accounts.length }} 个子账号)
            </span>
            <span
              v-if="group.hasActive"
              class="px-2 py-0.5 text-[10px] font-semibold rounded-full bg-success-soft text-success"
            >
              当前激活
            </span>
          </div>
          <div class="text-[11px] text-muted font-mono flex items-center gap-2 mt-0.5">
            <span>导入时间: {{ formatDate(group.createdAt) }}</span>
            <span>·</span>
            <span
              class="truncate max-w-[140px]"
              :title="group.id"
            >会话指纹: {{ String(group.id || '').replace(/^cookie_/, '') }}</span>
          </div>
        </div>
      </div>

      <!-- Group Summary & Group Actions -->
      <div
        class="flex items-center gap-3 shrink-0"
        @click.stop
      >
        <div class="hidden sm:flex items-center gap-2 text-xs font-mono">
          <span class="text-muted">调用: <strong class="text-fg">{{ group.totalRequests }}</strong></span>
          <span class="text-fg">|</span>
          <span :class="group.totalRateLimited > 0 ? 'text-danger font-semibold' : 'text-muted'">
            429: {{ group.totalRateLimited }}
          </span>
        </div>

        <button
          type="button"
          class="flex items-center gap-1 px-2 py-1 text-[11px] text-muted hover:text-danger hover:bg-danger-soft rounded-lg transition-colors cursor-pointer"
          title="删除整份 Cookie 及其下所有子账号"
          @click="emit('deleteGroup')"
        >
          <FolderMinus class="w-3.5 h-3.5" />
          <span class="hidden md:inline">删除整组</span>
        </button>
      </div>
    </div>

    <!-- Level 2: Sub-Accounts Container -->
    <div
      v-if="!collapsed"
      class="bg-canvas/40 px-4 sm:px-6 py-3 border-t border-line"
    >
      <div class="space-y-2">
        <slot />
      </div>
    </div>
  </div>
</template>
