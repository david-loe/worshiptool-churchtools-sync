<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import { api, errorMessage } from '@/api/client'
import type { Workspace } from '@/api/types'
import { useWorkspaceStore } from '@/stores/workspaces'

const route = useRoute()
const router = useRouter()
const workspaces = useWorkspaceStore()
const loading = ref(true)
const error = ref<string | null>(null)
const accepted = ref<Workspace | null>(null)

onMounted(async () => {
  const token = typeof route.query.token === 'string' ? route.query.token : ''
  if (!token) { error.value = 'Der Einladungslink ist unvollständig.'; loading.value = false; return }
  await router.replace({ path: route.path, query: {} })
  try {
    accepted.value = await api.post<Workspace>('/workspaces/invitations/accept', { token })
    await workspaces.load()
    workspaces.select(accepted.value.id)
  } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false }
})
</script>

<template><main class="not-found"><LoadingState v-if="loading" label="Einladung wird angenommen …" /><ErrorBanner v-else-if="error" :message="error" /><template v-else-if="accepted"><span>✓</span><h1>Willkommen bei {{ accepted.name }}</h1><p>Der Workspace wurde deinem Konto hinzugefügt.</p><button class="button button-primary" type="button" @click="router.push('/dashboard')">Workspace öffnen</button></template></main></template>
