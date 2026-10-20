import { defineStore } from 'pinia'
import { ref } from 'vue'
import { authApi } from '@/api/auth.ts'
import { ApiError, getToken, setToken, clearToken } from '@/api/client.ts'

export const useAuthStore = defineStore('auth', () => {
  const authEnabled = ref(false)
  const token = ref(getToken())
  const checking = ref(false)
  const initialized = ref(false)
  let authRevision = 0

  async function checkAuth(): Promise<boolean> {
    checking.value = true
    try {
      const res = await authApi.checkAuth()
      authEnabled.value = res.auth_enabled
      if (!res.auth_enabled) {
        return true
      }

      if (!token.value) {
        return false
      }

      const verifiedToken = token.value
      const verificationRevision = authRevision
      try {
        await authApi.verifyToken(verifiedToken)
        return authRevision === verificationRevision && token.value === verifiedToken && getToken() === verifiedToken
      } catch (error) {
        if (error instanceof ApiError && error.status === 401 && authRevision === verificationRevision && token.value === verifiedToken) {
          if (getToken() === verifiedToken) clearToken()
          token.value = getToken()
        }
        return false
      }
    } catch {
      return false
    } finally {
      checking.value = false
      initialized.value = true
    }
  }

  async function login(newToken: string): Promise<boolean> {
    const trimmed = newToken.trim()
    if (!trimmed) return false
    const loginRevision = ++authRevision
    const previousToken = getToken()

    try {
      await authApi.verifyToken(trimmed)
      if (authRevision !== loginRevision || getToken() !== previousToken) return false
      setToken(trimmed)
      token.value = trimmed
      return true
    } catch {
      return false
    }
  }

  function logout() {
    authRevision++
    clearToken()
    token.value = ''
  }

  function updateToken(newToken: string) {
    authRevision++
    const trimmed = newToken.trim()
    setToken(trimmed)
    token.value = trimmed
  }

  return {
    authEnabled,
    token,
    checking,
    initialized,
    checkAuth,
    login,
    logout,
    updateToken,
  }
})
