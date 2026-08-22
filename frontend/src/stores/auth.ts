import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, ApiError } from '@/api/client'
import type { RegisterResponse, SessionResponse, TotpSetup, User } from '@/api/types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const sessionChecked = ref(false)
  const loading = ref(false)
  const isAuthenticated = computed(() => user.value !== null)

  async function ensureSession(force = false): Promise<User | null> {
    if (sessionChecked.value && !force) return user.value
    loading.value = true
    try {
      user.value = await api.get<User>('/auth/me', { cache: 'no-store' })
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) throw error
      user.value = null
    } finally {
      loading.value = false
      sessionChecked.value = true
    }
    return user.value
  }

  async function login(email: string, password: string, totpCode?: string, recoveryCode?: string): Promise<User> {
    const result = await api.post<SessionResponse>('/auth/login', {
      email: email.trim().toLowerCase(),
      password,
      ...(totpCode ? { totp_code: totpCode } : {}),
      ...(recoveryCode ? { recovery_code: recoveryCode } : {}),
    })
    user.value = result.user
    sessionChecked.value = true
    return result.user
  }

  async function register(email: string, password: string, workspaceName?: string): Promise<RegisterResponse> {
    return api.post<RegisterResponse>('/auth/register', {
      email: email.trim().toLowerCase(),
      password,
      ...(workspaceName?.trim() ? { workspace_name: workspaceName.trim() } : {}),
    })
  }

  async function logout(): Promise<void> {
    try {
      await api.post<void>('/auth/logout')
    } finally {
      user.value = null
      sessionChecked.value = true
      api.activateWorkspace(null)
    }
  }

  async function startTotp(password: string, code?: string, recoveryCode?: string): Promise<TotpSetup> {
    return api.post<TotpSetup>('/auth/totp/setup', {
      password,
      ...(code ? { code } : {}),
      ...(recoveryCode ? { recovery_code: recoveryCode } : {}),
    })
  }

  async function confirmTotp(code: string): Promise<string[]> {
    const result = await api.post<{ recovery_codes: string[] }>('/auth/totp/confirm', { code })
    if (user.value) user.value = { ...user.value, totp_enabled: true }
    return result.recovery_codes
  }

  async function disableTotp(password: string, code?: string, recoveryCode?: string): Promise<void> {
    await api.post<void>('/auth/totp/disable', {
      password,
      ...(code ? { code } : {}),
      ...(recoveryCode ? { recovery_code: recoveryCode } : {}),
    })
    if (user.value) user.value = { ...user.value, totp_enabled: false }
  }

  return { user, sessionChecked, loading, isAuthenticated, ensureSession, login, register, logout, startTotp, confirmTotp, disableTotp }
})
