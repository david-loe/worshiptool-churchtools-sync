import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api/client'
import type { Workspace } from '@/api/types'

const STORAGE_KEY = 'wt-sync:active-workspace'

export const useWorkspaceStore = defineStore('workspaces', () => {
  const workspaces = ref<Workspace[]>([])
  const activeId = ref<string | null>(null)
  const loading = ref(false)
  const active = computed(() => workspaces.value.find((item) => item.id === activeId.value) ?? null)
  const canManage = computed(() => active.value?.role === 'owner' || active.value?.role === 'admin')
  const canOperate = computed(() => canManage.value || active.value?.role === 'operator')

  async function load(): Promise<Workspace[]> {
    loading.value = true
    try {
      workspaces.value = await api.allPages<Workspace>('/workspaces', { cache: 'no-store' })
      const stored = localStorage.getItem(STORAGE_KEY)
      const candidate = workspaces.value.find((item) => item.id === stored)?.id ?? workspaces.value[0]?.id ?? null
      select(candidate)
      return workspaces.value
    } finally {
      loading.value = false
    }
  }

  function select(workspaceId: string | null): void {
    const validId = workspaceId && workspaces.value.some((item) => item.id === workspaceId) ? workspaceId : null
    activeId.value = validId
    api.activateWorkspace(validId)
    if (validId) localStorage.setItem(STORAGE_KEY, validId)
    else localStorage.removeItem(STORAGE_KEY)
  }

  async function create(name: string): Promise<Workspace> {
    const workspace = await api.post<Workspace>('/workspaces', { name: name.trim() })
    workspaces.value.push(workspace)
    select(workspace.id)
    return workspace
  }

  function reset(): void {
    workspaces.value = []
    activeId.value = null
    api.activateWorkspace(null)
    localStorage.removeItem(STORAGE_KEY)
  }

  return { workspaces, activeId, active, loading, canManage, canOperate, load, select, create, reset }
})
