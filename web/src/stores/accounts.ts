import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { accountsApi } from '@/api/accounts.ts'
import { useSystemStore } from './system.ts'
import type {
  Account,
  AccountWithStats,
  AccountRotationStats,
  ImportCookiesRequest,
  ProbeAndImportRequest,
  ImportBundleRequest,
} from '@/types'
import { useToastStore } from './toast.ts'

export const useAccountsStore = defineStore('accounts', () => {
  const accounts = ref<Account[]>([])
  const activeAccount = ref<Account | null>(null)
  const systemStore = useSystemStore()
  const rotationAccounts = computed<Record<string, AccountRotationStats>>(() => {
    return systemStore.rotation?.accounts || {}
  })
  const loading = ref(false)
  const activatingId = ref<string | null>(null)
  const importing = ref(false)
  let refreshRequest = 0
  let activationRequest = 0

  const activeId = computed(() => activeAccount.value?.id || '')

  const accountRows = computed<AccountWithStats[]>(() => {
    return accounts.value.map((a) => {
      const rot = rotationAccounts.value[a.id] || {}
      return {
        ...a,
        ...rot,
      }
    })
  })

  async function fetchAll() {
    const request = ++refreshRequest
    loading.value = true
    try {
      const [accsRes, activeRes] = await Promise.allSettled([
        accountsApi.list(),
        accountsApi.getActive(),
        systemStore.fetchRotation(),
      ])
      if (request !== refreshRequest) return

      if (accsRes.status === 'fulfilled') {
        accounts.value = accsRes.value
      } else {
        const reasonMsg = accsRes.reason instanceof Error ? accsRes.reason.message : String(accsRes.reason)
        console.warn('获取账号列表失败，保留当前展示数据:', accsRes.reason)
        useToastStore().error(`获取账号列表失败: ${reasonMsg}`)
      }

      if (activeRes.status === 'fulfilled') {
        activeAccount.value = activeRes.value
      } else if (accsRes.status === 'fulfilled' &&
          !accounts.value.some(account => account.id === activeAccount.value?.id)) {
        activeAccount.value = null
      }
    } finally {
      if (request === refreshRequest) loading.value = false
    }
  }

  async function activateAccount(id: string) {
    const toast = useToastStore()
    const request = ++activationRequest
    ++refreshRequest
    activatingId.value = id
    try {
      const updated = await accountsApi.activate(id)
      if (request !== activationRequest) return
      activeAccount.value = updated
      toast.success(`已成功激活账号: ${updated.name || updated.email || updated.id}`)
      await fetchAll()
    } catch (err: unknown) {
      if (request !== activationRequest) return
      const msg = err instanceof Error ? err.message : '激活失败'
      toast.error(msg)
      await fetchAll()
    } finally {
      if (request === activationRequest) activatingId.value = null
    }
  }

  async function deleteAccount(id: string) {
    const toast = useToastStore()
    try {
      await accountsApi.delete(id)
      toast.success('账号已删除')
      await fetchAll()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '删除失败'
      toast.error(msg)
    }
  }

  async function deleteCookieGroup(cookieId: string) {
    const toast = useToastStore()
    try {
      const res = await accountsApi.deleteCookieGroup(cookieId)
      toast.success(`已成功删除该 Cookie 凭据下的全部 ${res.deleted} 个子账号`)
      await fetchAll()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '删除失败'
      toast.error(msg)
    }
  }

  async function updateAccountName(id: string, name: string) {
    const toast = useToastStore()
    try {
      await accountsApi.update(id, name)
      toast.success('账号名称已更新')
      await fetchAll()
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '更新失败'
      toast.error(msg)
      return false
    }
  }

  async function importCookies(payload: ImportCookiesRequest) {
    const toast = useToastStore()
    importing.value = true
    try {
      const res = await accountsApi.importCookies(payload)
      toast.success(`导入成功: 解析出 ${res.cookie_count} 个 Cookie (u/${res.auth_user})`)
      await fetchAll()
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '导入失败'
      toast.error(msg)
      return false
    } finally {
      importing.value = false
    }
  }

  async function probeAndImport(payload: ProbeAndImportRequest) {
    const toast = useToastStore()
    importing.value = true
    try {
      const res = await accountsApi.probeAndImport(payload)
      toast.success(`已完成探测并导入 ${res.imported_count} 个账号`)
      await fetchAll()
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '自动探活导入失败'
      toast.error(msg)
      return false
    } finally {
      importing.value = false
    }
  }

  async function importBundle(payload: ImportBundleRequest) {
    const toast = useToastStore()
    importing.value = true
    try {
      const res = await accountsApi.importBundle(payload)
      toast.success(`批量导入成功: 已入库 ${res.imported_count} 个账号`)
      await fetchAll()
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '批量导入失败'
      toast.error(msg)
      return false
    } finally {
      importing.value = false
    }
  }

  async function detectAccountEmail(id: string) {
    const toast = useToastStore()
    try {
      const res = await accountsApi.detectEmail(id)
      if (res.email) {
        toast.success(`已成功识别账号邮箱: ${res.email}`)
      } else {
        toast.info('未在当前页面识别到有效邮箱')
      }
      await fetchAll()
      return res
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '识别邮箱失败'
      toast.error(msg)
      return null
    }
  }

  return {
    accounts,
    activeAccount,
    activeId,
    rotationAccounts,
    accountRows,
    loading,
    activatingId,
    importing,
    fetchAll,
    activateAccount,
    deleteAccount,
    deleteCookieGroup,
    updateAccountName,
    importCookies,
    probeAndImport,
    importBundle,
    detectAccountEmail,
  }
})
