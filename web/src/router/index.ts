import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth.ts'
const LoginView = () => import('@/views/LoginView.vue')
const DashboardView = () => import('@/views/DashboardView.vue')
const AccountsView = () => import('@/views/AccountsView.vue')
const SettingsView = () => import('@/views/SettingsView.vue')
const RequestsView = () => import('@/views/RequestsView.vue')
const LogsView = () => import('@/views/LogsView.vue')
const UsageView = () => import('@/views/UsageView.vue')

const routes: RouteRecordRaw[] = [
  { path: '/usage', name: 'usage', component: UsageView, meta: { title: '用量统计' } },
  { path: '/requests', name: 'requests', component: RequestsView, meta: { title: '请求' } },
  { path: '/logs', name: 'logs', component: LogsView, meta: { title: '日志' } },
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    name: 'dashboard',
    component: DashboardView,
    meta: { title: '控制面板' },
  },
  {
    path: '/accounts',
    name: 'accounts',
    component: AccountsView,
    meta: { title: '账号管理' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: SettingsView,
    meta: { title: '系统配置' },
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to, _from, next) => {
  // 设置文档标题
  if (to.meta.title && typeof to.meta.title === 'string') {
    document.title = `${to.meta.title} - AI Studio Proxy`
  }

  const authStore = useAuthStore()

  // 初始化鉴权状态
  if (!authStore.initialized) {
    await authStore.checkAuth()
  }

  const isPublic = to.meta.public === true

  if (authStore.authEnabled) {
    if (!authStore.token) {
      if (!isPublic) {
        return next({ name: 'login', query: { redirect: to.fullPath } })
      }
    } else {
      if (to.name === 'login') {
        return next({ name: 'dashboard' })
      }
    }
  } else {
    if (to.name === 'login') {
      return next({ name: 'dashboard' })
    }
  }

  next()
})
