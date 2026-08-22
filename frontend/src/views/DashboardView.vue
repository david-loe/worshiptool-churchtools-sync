<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import EmptyState from '@/components/EmptyState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, ApiError, errorMessage, pagePath } from '@/api/client'
import type { AppNotification, Connection, Page, SyncProfile, SyncRun } from '@/api/types'
import { recoverableRunId } from '@/domain/run'
import { formatDateTime, formatRelative } from '@/utils/format'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useToastStore } from '@/stores/toasts'

const workspaceStore = useWorkspaceStore()
const toasts = useToastStore()
const router = useRouter()
const loading = ref(true)
const error = ref<string | null>(null)
const profiles = ref<SyncProfile[]>([])
const runs = ref<SyncRun[]>([])
const connections = ref<Connection[]>([])
const notifications = ref<AppNotification[]>([])
const unread = ref(0)
const startingProfileId = ref<string | null>(null)

const successfulRuns = computed(() => runs.value.filter((run) => run.status === 'succeeded').length)
const lastRun = computed(() => runs.value[0] ?? null)
const activeProfiles = computed(() => profiles.value.filter((profile) => profile.enabled))
const connectionHealth = computed(() => {
  if (!connections.value.length) return 'unknown'
  if (connections.value.some((connection) => connection.last_test_succeeded === false)) return 'error'
  return connections.value.every((connection) => connection.last_test_succeeded === true) ? 'healthy' : 'unknown'
})

function lastRunFor(profileId: string): SyncRun | undefined {
  return runs.value.find((run) => run.profile_id === profileId)
}

async function load(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  loading.value = true
  error.value = null
  try {
    const [profileItems, runPage, connectionItems, notificationPage] = await Promise.all([
      api.allPages<SyncProfile>(`/workspaces/${workspaceId}/profiles`, { workspaceId, cacheForMs: 10_000 }),
      api.page<SyncRun>(pagePath(`/workspaces/${workspaceId}/runs`, 20), { workspaceId, cacheForMs: 5_000 }),
      api.allPages<Connection>(`/workspaces/${workspaceId}/connections`, { workspaceId, cacheForMs: 10_000 }),
      api.get<Page<AppNotification> & { unread: number }>(pagePath(`/workspaces/${workspaceId}/notifications`, 5), { workspaceId, cacheForMs: 5_000 }),
    ])
    profiles.value = profileItems
    runs.value = runPage.items
    connections.value = connectionItems
    notifications.value = notificationPage.items
    unread.value = notificationPage.unread
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function start(profile: SyncProfile): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  startingProfileId.value = profile.id
  try {
    const run = await api.post<SyncRun>(`/workspaces/${workspaceId}/profiles/${profile.id}/runs`, { dry_run: false })
    toasts.show('success', 'Sync wurde eingereiht', `Lauf ${run.id.slice(0, 8)} wartet auf einen Worker.`)
    await router.push(`/runs/${run.id}`)
  } catch (cause) {
    const existingRunId = recoverableRunId(cause, workspaceId)
    if (existingRunId) {
      const delayed = cause instanceof ApiError && cause.hasCode('queue_unavailable')
      toasts.show(
        delayed ? 'warning' : 'info',
        delayed ? 'Sync gespeichert, Queue verzögert' : 'Für dieses Profil läuft bereits ein Sync',
        delayed ? 'Der persistierte Lauf wird automatisch erneut eingereiht.' : undefined,
      )
      await router.push(`/runs/${existingRunId}`)
    } else if (cause instanceof ApiError && cause.status === 429) {
      const minutes = Math.max(1, Math.ceil((cause.retryAfter ?? 60) / 60))
      toasts.show('warning', 'Manueller Start noch gesperrt', `Bitte versuche es in ${minutes} Minuten erneut.`)
    } else if (cause instanceof ApiError && cause.status === 409) {
      toasts.show('info', 'Für dieses Profil läuft bereits ein Sync', cause.message)
    } else {
      toasts.show('error', 'Sync konnte nicht gestartet werden', errorMessage(cause))
    }
  } finally {
    startingProfileId.value = null
  }
}

onMounted(load)
</script>

<template>
  <PageHeader :title="`Hallo${workspaceStore.active ? ` bei ${workspaceStore.active.name}` : ''}`" eyebrow="Übersicht" description="Alle Verbindungen, Profile und letzten Läufe auf einen Blick.">
    <RouterLink v-if="workspaceStore.canManage" class="button button-secondary" to="/profiles/new">Profil anlegen</RouterLink>
  </PageHeader>
  <LoadingState v-if="loading" />
  <ErrorBanner v-else-if="error" :message="error" />
  <template v-else>
    <section class="metric-grid" aria-label="Statusübersicht">
      <article class="metric-card"><span class="metric-icon success">✓</span><div><small>Letzter Lauf</small><strong>{{ lastRun ? formatRelative(lastRun.finished_at || lastRun.created_at) : 'Noch keiner' }}</strong><StatusBadge v-if="lastRun" :status="lastRun.status" /></div></article>
      <article class="metric-card"><span class="metric-icon">⇄</span><div><small>Aktive Profile</small><strong>{{ activeProfiles.length }}</strong><span>von {{ profiles.length }} Profilen</span></div></article>
      <article class="metric-card"><span class="metric-icon" :class="connectionHealth === 'error' ? 'danger' : ''">⌁</span><div><small>Verbindungen</small><strong>{{ connections.length }}</strong><StatusBadge :status="connectionHealth" /></div></article>
      <article class="metric-card"><span class="metric-icon">◉</span><div><small>Ungelesen</small><strong>{{ unread }}</strong><RouterLink to="/notifications">Benachrichtigungen</RouterLink></div></article>
    </section>

    <div class="dashboard-grid">
      <section class="card panel-main">
        <div class="section-heading"><div><h2>Sync-Profile</h2><p>{{ successfulRuns }} erfolgreiche Läufe in der aktuellen Ansicht</p></div><RouterLink to="/profiles">Alle Profile</RouterLink></div>
        <EmptyState v-if="!profiles.length" title="Noch kein Profil eingerichtet" text="Verbinde WorshipTools mit ChurchTools und prüfe deinen ersten Lauf." symbol="⇄"><RouterLink v-if="workspaceStore.canManage" class="button button-primary" to="/onboarding">Einrichtung starten</RouterLink></EmptyState>
        <div v-else class="profile-list">
          <article v-for="profile in profiles.slice(0, 5)" :key="profile.id" class="profile-row">
            <div class="profile-state" :class="{ active: profile.enabled }"><span /></div>
            <div class="profile-summary"><RouterLink v-if="workspaceStore.canManage" :to="`/profiles/${profile.id}`"><strong>{{ profile.name }}</strong></RouterLink><strong v-else>{{ profile.name }}</strong><span>{{ profile.schedule_type === 'interval' ? `alle ${profile.interval_minutes} Minuten` : profile.cron_expression }} · {{ profile.lookahead_days }} Tage Vorschau</span></div>
            <div class="profile-last"><small>Letzter Lauf</small><StatusBadge v-if="lastRunFor(profile.id)" :status="lastRunFor(profile.id)!.status" /><span v-else>–</span></div>
            <button v-if="workspaceStore.canOperate" class="button button-small button-secondary" type="button" :disabled="startingProfileId === profile.id || !profile.enabled" @click="start(profile)">{{ startingProfileId === profile.id ? 'Startet …' : 'Jetzt syncen' }}</button>
          </article>
        </div>
      </section>

      <aside class="card panel-side">
        <div class="section-heading"><div><h2>Neuigkeiten</h2><p>Aus deinem Workspace</p></div><RouterLink to="/notifications">Alle</RouterLink></div>
        <EmptyState v-if="!notifications.length" title="Alles ruhig" text="Neue Hinweise erscheinen hier." symbol="✓" />
        <ul v-else class="notification-mini-list"><li v-for="notification in notifications" :key="notification.id" :class="notification.severity"><span class="notification-dot" /><div><strong>{{ notification.title }}</strong><p>{{ notification.body }}</p><small>{{ formatDateTime(notification.created_at) }}</small></div></li></ul>
      </aside>
    </div>
  </template>
</template>
