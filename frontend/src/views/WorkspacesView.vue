<script setup lang="ts">
import { onMounted, ref } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import { api, errorMessage } from '@/api/client'
import type { Role, WorkspaceInvitation, WorkspaceMember } from '@/api/types'
import { formatDateTime } from '@/utils/format'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useToastStore } from '@/stores/toasts'

const workspaceStore = useWorkspaceStore()
const toasts = useToastStore()
const members = ref<WorkspaceMember[]>([])
const invitations = ref<WorkspaceInvitation[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const newWorkspaceName = ref('')
const inviteEmail = ref('')
const inviteRole = ref<Role>('viewer')
const saving = ref(false)
const roleLabels: Record<Role, string> = { owner: 'Eigentümer', admin: 'Administrator', operator: 'Operator', viewer: 'Betrachter' }

async function loadMembers(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  loading.value = true
  try {
    const [memberData, invitationData] = await Promise.all([
      api.get<WorkspaceMember[]>(`/workspaces/${workspaceId}/members`, { workspaceId }),
      workspaceStore.canManage ? api.get<WorkspaceInvitation[]>(`/workspaces/${workspaceId}/invitations`, { workspaceId }) : Promise.resolve([]),
    ])
    members.value = memberData
    invitations.value = invitationData.filter((item) => !item.accepted_at)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function createWorkspace(): Promise<void> {
  if (!newWorkspaceName.value.trim()) return
  saving.value = true
  try {
    await workspaceStore.create(newWorkspaceName.value)
    newWorkspaceName.value = ''
    await loadMembers()
    toasts.show('success', 'Workspace erstellt')
  } catch (cause) {
    toasts.show('error', 'Workspace konnte nicht erstellt werden', errorMessage(cause))
  } finally { saving.value = false }
}

async function rename(): Promise<void> {
  const workspace = workspaceStore.active
  if (!workspace) return
  const name = prompt('Neuer Workspace-Name', workspace.name)?.trim()
  if (!name || name === workspace.name) return
  try {
    await api.patch(`/workspaces/${workspace.id}`, { name })
    await workspaceStore.load()
    toasts.show('success', 'Workspace umbenannt')
  } catch (cause) { toasts.show('error', 'Name konnte nicht geändert werden', errorMessage(cause)) }
}

async function changeRole(member: WorkspaceMember, event: Event): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  const role = (event.target as HTMLSelectElement).value as Role
  try {
    const updated = await api.patch<WorkspaceMember>(`/workspaces/${workspaceId}/members/${member.id}`, { role })
    Object.assign(member, updated)
    toasts.show('success', 'Rolle aktualisiert')
  } catch (cause) {
    ;(event.target as HTMLSelectElement).value = member.role
    toasts.show('error', 'Rolle konnte nicht geändert werden', errorMessage(cause))
  }
}

async function invite(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  saving.value = true
  try {
    await api.post(`/workspaces/${workspaceId}/invitations`, { email: inviteEmail.value.trim().toLowerCase(), role: inviteRole.value })
    toasts.show('success', 'Einladung versendet')
    inviteEmail.value = ''
    await loadMembers()
  } catch (cause) {
    toasts.show('error', 'Einladung konnte nicht versendet werden', errorMessage(cause))
  } finally { saving.value = false }
}

async function revokeInvitation(invitation: WorkspaceInvitation): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  try {
    await api.delete(`/workspaces/${workspaceId}/invitations/${invitation.id}`)
    invitations.value = invitations.value.filter((item) => item.id !== invitation.id)
    toasts.show('success', 'Einladung zurückgezogen')
  } catch (cause) { toasts.show('error', 'Einladung konnte nicht zurückgezogen werden', errorMessage(cause)) }
}

onMounted(loadMembers)
</script>

<template>
  <PageHeader title="Workspace & Team" eyebrow="Mandantenverwaltung" description="Daten und Browser-Caches sind zwischen Workspaces strikt getrennt."><button v-if="workspaceStore.canManage" class="button button-secondary" type="button" @click="rename">Workspace umbenennen</button></PageHeader>
  <ErrorBanner v-if="error" :message="error" />
  <section class="workspace-grid">
    <div class="card workspace-list-panel"><div class="section-heading"><div><h2>Meine Workspaces</h2><p>{{ workspaceStore.workspaces.length }} verfügbar</p></div></div><ul class="workspace-list"><li v-for="workspace in workspaceStore.workspaces" :key="workspace.id" :class="{ active: workspace.id === workspaceStore.activeId }"><button type="button" @click="workspaceStore.select(workspace.id); loadMembers()"><span class="workspace-avatar">{{ workspace.name.slice(0, 2).toUpperCase() }}</span><span><strong>{{ workspace.name }}</strong><small>{{ roleLabels[workspace.role] }} · max. {{ workspace.profile_quota }} Profile</small></span><span v-if="workspace.id === workspaceStore.activeId">✓</span></button></li></ul><form class="inline-create" @submit.prevent="createWorkspace"><label><span>Neuer Workspace</span><input v-model="newWorkspaceName" required placeholder="Name der Gemeinde" /></label><button class="button button-secondary" type="submit" :disabled="saving">Anlegen</button></form></div>
    <div class="card team-panel"><div class="section-heading"><div><h2>Teammitglieder</h2><p>{{ members.length }} von {{ workspaceStore.active?.member_quota ?? '–' }} Plätzen belegt</p></div></div><LoadingState v-if="loading" /><div v-else class="table-card flat"><table><thead><tr><th>Mitglied</th><th>Rolle</th><th>Seit</th></tr></thead><tbody><tr v-for="member in members" :key="member.id"><td><strong>{{ member.email }}</strong></td><td><select :value="member.role" :disabled="!workspaceStore.canManage" :aria-label="`Rolle von ${member.email}`" @change="changeRole(member, $event)"><option value="owner" :disabled="workspaceStore.active?.role !== 'owner'">Eigentümer</option><option value="admin">Administrator</option><option value="operator">Operator</option><option value="viewer">Betrachter</option></select></td><td>{{ formatDateTime(member.created_at) }}</td></tr><tr v-for="invitation in invitations" :key="invitation.id" class="invitation-row"><td><strong>{{ invitation.email }}</strong><small class="table-sub">Einladung ausstehend</small></td><td>{{ roleLabels[invitation.role] }}</td><td><button class="link-button danger-text" type="button" @click="revokeInvitation(invitation)">Zurückziehen</button></td></tr></tbody></table></div><form v-if="workspaceStore.canManage" class="invite-form" @submit.prevent="invite"><h3>Mitglied einladen</h3><label><span>E-Mail-Adresse</span><input v-model="inviteEmail" type="email" required /></label><label><span>Rolle</span><select v-model="inviteRole"><option value="admin">Administrator</option><option value="operator">Operator</option><option value="viewer">Betrachter</option></select></label><button class="button button-primary" type="submit" :disabled="saving">Einladen</button></form></div>
  </section>
</template>
