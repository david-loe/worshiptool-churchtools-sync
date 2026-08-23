<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import EmptyState from '@/components/EmptyState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, ApiError, errorMessage, pagePath } from '@/api/client'
import type { SyncProfile, SyncRun } from '@/api/types'
import { recoverableRunId } from '@/domain/run'
import { formatDateTime } from '@/utils/format'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useToastStore } from '@/stores/toasts'

const workspaceStore = useWorkspaceStore()
const toasts = useToastStore()
const router = useRouter()
const profiles = ref<SyncProfile[]>([])
const runs = ref<SyncRun[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const busyId = ref<string | null>(null)
const offset = ref(0)
const total = ref(0)
const limit = 50
const pageNumber = computed(() => Math.floor(offset.value / limit) + 1)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / limit)))

function latestRun(profileId: string): SyncRun | undefined {
  return runs.value.find((run) => run.profile_id === profileId)
}

function deleteBlockedReason(profile: SyncProfile): string | null {
  const blockers = profile.delete_blockers ?? []
  if (blockers.includes('run_history')) return 'Dieses Profil besitzt Laufhistorie und bleibt deshalb aus Audit-Gründen erhalten.'
  if (blockers.includes('remote_binding')) return 'Dieses Profil besitzt noch verwaltete Remote-Zuordnungen.'
  return null
}

async function load(): Promise<void> {
  const id = workspaceStore.activeId
  if (!id) return
  loading.value = true
  error.value = null
  try {
    const [profilePage, runPage] = await Promise.all([
      api.page<SyncProfile>(pagePath(`/workspaces/${id}/profiles`, limit, offset.value), { workspaceId: id }),
      api.page<SyncRun>(pagePath(`/workspaces/${id}/runs`, 200), { workspaceId: id }),
    ])
    profiles.value = profilePage.items
    total.value = profilePage.total
    runs.value = runPage.items
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function toggle(profile: SyncProfile): Promise<void> {
  const id = workspaceStore.activeId
  if (!id) return
  busyId.value = profile.id
  try {
    await api.patch<SyncProfile>(`/workspaces/${id}/profiles/${profile.id}`, { enabled: !profile.enabled })
    await load()
  } catch (cause) {
    toasts.show('error', 'Profil konnte nicht geändert werden', errorMessage(cause))
  } finally {
    busyId.value = null
  }
}

async function start(profile: SyncProfile): Promise<void> {
  const id = workspaceStore.activeId
  if (!id) return
  busyId.value = profile.id
  try {
    const run = await api.post<SyncRun>(`/workspaces/${id}/profiles/${profile.id}/runs`, { dry_run: false })
    await router.push(`/runs/${run.id}`)
  } catch (cause) {
    const existingRunId = recoverableRunId(cause, id)
    if (existingRunId) {
      const delayed = cause instanceof ApiError && cause.hasCode('queue_unavailable')
      toasts.show(
        delayed ? 'warning' : 'info',
        delayed ? 'Sync gespeichert, Queue verzögert' : 'Sync läuft bereits',
        delayed ? 'Der persistierte Lauf wird automatisch erneut eingereiht.' : undefined,
      )
      await router.push(`/runs/${existingRunId}`)
    } else {
      const message = cause instanceof ApiError && cause.status === 429
        ? `Der Cooldown läuft noch. Erneut möglich in ${Math.ceil((cause.retryAfter ?? 60) / 60)} Minuten.`
        : errorMessage(cause)
      toasts.show('warning', 'Sync nicht gestartet', message)
    }
  } finally {
    busyId.value = null
  }
}

async function preview(profile: SyncProfile): Promise<void> {
  const id = workspaceStore.activeId
  if (!id) return
  busyId.value = profile.id
  try {
    const run = await api.post<SyncRun>(`/workspaces/${id}/profiles/${profile.id}/preview`)
    await router.push(`/runs/${run.id}`)
  } catch (cause) {
    const existingRunId = recoverableRunId(cause, id)
    if (existingRunId) {
      toasts.show('warning', 'Vorschau ist bereits gespeichert', 'Der vorhandene Lauf wird geöffnet.')
      await router.push(`/runs/${existingRunId}`)
    } else {
      toasts.show('error', 'Vorschau konnte nicht gestartet werden', errorMessage(cause))
    }
  } finally {
    busyId.value = null
  }
}

async function remove(profile: SyncProfile): Promise<void> {
  const blockedReason = deleteBlockedReason(profile)
  if (blockedReason) {
    toasts.show('warning', 'Profil kann nicht gelöscht werden', blockedReason)
    return
  }
  if (!confirm(`Unbenutztes Profil „${profile.name}“ dauerhaft löschen?`)) return
  const id = workspaceStore.activeId
  if (!id) return
  try {
    await api.delete(`/workspaces/${id}/profiles/${profile.id}`)
    toasts.show('success', 'Profil gelöscht')
    if (profiles.value.length === 1 && offset.value > 0) offset.value = Math.max(0, offset.value - limit)
    await load()
  } catch (cause) {
    toasts.show('error', 'Profil konnte nicht gelöscht werden', errorMessage(cause))
  }
}

function changePage(direction: -1 | 1): void {
  offset.value = Math.max(0, offset.value + direction * limit)
  void load()
}

onMounted(load)
</script>

<template>
  <PageHeader title="Sync-Profile" eyebrow="Automatisierung" description="Jedes Profil verbindet genau eine WorshipTools-Quelle mit einem ChurchTools-Ziel.">
    <RouterLink v-if="workspaceStore.canManage" class="button button-primary" to="/profiles/new">Neues Profil</RouterLink>
  </PageHeader>
  <ErrorBanner v-if="error" :message="error" />
  <LoadingState v-if="loading" />
  <EmptyState v-else-if="!profiles.length && total === 0" title="Noch kein Sync-Profil" text="Lege Regeln, Zeitplan und Benachrichtigungen für deine erste Verbindung fest." symbol="⇄"><RouterLink v-if="workspaceStore.canManage" class="button button-primary" to="/profiles/new">Profil anlegen</RouterLink></EmptyState>
  <section v-else class="profile-card-list">
    <article v-for="profile in profiles" :key="profile.id" class="card profile-card">
      <header><div class="profile-state" :class="{ active: profile.enabled }"><span /></div><div><RouterLink v-if="workspaceStore.canManage" :to="`/profiles/${profile.id}`"><h2>{{ profile.name }}</h2></RouterLink><h2 v-else>{{ profile.name }}</h2><span>{{ profile.enabled ? 'Automatischer Sync aktiv' : 'Pausiert' }}</span></div><StatusBadge v-if="latestRun(profile.id)" :status="latestRun(profile.id)!.status" /></header>
      <dl class="profile-facts"><div><dt>Zeitplan</dt><dd>{{ profile.schedule_type === 'interval' ? `Alle ${profile.interval_minutes} Minuten` : profile.cron_expression }}</dd></div><div><dt>Vorschau</dt><dd>{{ profile.lookahead_days }} Tage</dd></div><div><dt>Event-Matching</dt><dd>{{ profile.match_mode === 'exact_time' ? 'Exakte Startzeit' : 'Lokales Datum' }}</dd></div><div><dt>Letzter Lauf</dt><dd>{{ formatDateTime(latestRun(profile.id)?.finished_at || latestRun(profile.id)?.created_at) }}</dd></div></dl>
      <footer><div v-if="workspaceStore.canManage" class="toggle-wrap"><button class="switch" :class="{ checked: profile.enabled }" type="button" role="switch" :aria-checked="profile.enabled" :aria-label="`${profile.name} ${profile.enabled ? 'pausieren' : 'aktivieren'}`" :disabled="busyId === profile.id" @click="toggle(profile)"><span /></button><span>{{ profile.enabled ? 'Aktiv' : 'Pausiert' }}</span></div><div class="row-actions"><button v-if="workspaceStore.canOperate" class="button button-small button-secondary" type="button" :disabled="busyId === profile.id" @click="preview(profile)">Vorschau</button><button v-if="workspaceStore.canOperate" class="button button-small button-secondary" type="button" :disabled="busyId === profile.id || !profile.enabled" @click="start(profile)">Jetzt syncen</button><RouterLink v-if="workspaceStore.canManage" class="button button-small button-secondary" :to="`/profiles/${profile.id}`">Bearbeiten</RouterLink><button v-if="workspaceStore.canManage" class="link-button danger-text" type="button" :disabled="Boolean(deleteBlockedReason(profile))" :title="deleteBlockedReason(profile) ?? 'Profil löschen'" @click="remove(profile)">Löschen</button></div></footer>
      <small v-if="deleteBlockedReason(profile)" class="table-sub">{{ deleteBlockedReason(profile) }}</small>
    </article>
  </section>
  <nav v-if="total > limit" class="pagination" aria-label="Profilseiten"><button class="button button-small button-secondary" type="button" :disabled="offset === 0" @click="changePage(-1)">Zurück</button><span>Seite {{ pageNumber }} von {{ totalPages }}</span><button class="button button-small button-secondary" type="button" :disabled="offset + limit >= total" @click="changePage(1)">Weiter</button></nav>
</template>
