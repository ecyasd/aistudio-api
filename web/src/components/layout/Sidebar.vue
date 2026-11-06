<script setup lang="ts">
import { Activity, LayoutDashboard, Users, Sliders, Key, LogOut, ScrollText, Radio, ChartNoAxesCombined } from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { useRouter, useRoute } from 'vue-router'
import { useSystemHealth } from '@/composables/useSystemHealth'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [value: boolean]; openTokenModal: [] }>()
const authStore = useAuthStore()
const router = useRouter()
const route = useRoute()
const { state: healthState } = useSystemHealth(15000)
const navItems = [
  { name: '概览', path: '/', icon: LayoutDashboard },
  { name: '用量统计', path: '/usage', icon: ChartNoAxesCombined },
  { name: '请求', path: '/requests', icon: Activity },
  { name: '日志', path: '/logs', icon: ScrollText },
  { name: '账号', path: '/accounts', icon: Users },
  { name: '配置', path: '/settings', icon: Sliders },
]
function logout() { authStore.logout(); void router.push('/login') }
</script>

<template>
  <button
    v-if="open"
    type="button"
    class="console-mobile-backdrop fixed inset-0 z-30 bg-black/60"
    aria-label="关闭导航"
    @click="emit('update:open', false)"
  />
  <aside
    class="console-sidebar"
    :class="{ 'is-open': open }"
  >
    <div class="console-brand">
      <Radio :size="26" /><div>AI Studio<small>API CONSOLE</small></div>
    </div>
    <nav
      class="console-nav"
      aria-label="主导航"
    >
      <span class="console-nav-title">WORKSPACE</span>
      <RouterLink
        v-for="item in navItems"
        :key="item.path"
        :to="item.path"
        :class="{ active: route.path === item.path }"
        :aria-current="route.path === item.path ? 'page' : undefined"
        @click="emit('update:open', false)"
      ><component
        :is="item.icon"
        :size="17"
      />{{ item.name }}</RouterLink>
    </nav>
    <div class="sidebar-spacer" />
    <footer class="sidebar-footer">
      <div>
        <span
          class="sidebar-dot"
          :style="{ opacity: healthState === 'online' ? 1 : .3 }"
        />{{ healthState === 'online' ? '服务在线' : healthState === 'offline' ? '服务离线' : '正在检查' }}
      </div>
      <div>
        <button
          type="button"
          class="console-button"
          @click="emit('openTokenModal')"
        >
          <Key :size="14" />管理凭据
        </button><button
          type="button"
          class="console-button"
          aria-label="退出登录"
          @click="logout"
        >
          <LogOut :size="15" />
        </button>
      </div>
    </footer>
  </aside>
</template>
