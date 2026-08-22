<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, errorMessage } from '@/api/client'
import type { SyncAction, SyncActionPage, SyncRun } from '@/api/types'
import { duration, formatDateTime } from '@/utils/format'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useToastStore } from '@/stores/toasts'

const route = useRoute()
const workspaceStore = useWorkspaceStore()
const toasts = useToastStore()
const run = ref<SyncRun | null>(null)
const actions = ref<SyncActionPage | null>(null)
const actionOffset = ref(0)
const actionLimit = 50
const loading = ref(true)
const error = ref<string | null>(null)
const expandedAction = ref<string | null>(null)
let pollTimer: number | undefined
const active = computed(() => run.value?.status === 'queued' || run.value?.status === 'running')
const actionCounts = computed(() => actions.value?.status_counts)
const actionPageStart = computed(() => actions.value?.total ? actions.value.offset + 1 : 0)
const actionPageEnd = computed(() => actions.value ? Math.min(actions.value.total, actions.value.offset + actions.value.items.length) : 0)

function actionDescription(action: SyncAction): string {
  const value = action.payload.description ?? action.payload.name ?? action.payload.song_name ?? action.kind
  return String(value)
}

function errorDetail(value: Record<string, unknown> | null): string {
  if (!value) return ''
  return String(value.message ?? value.detail ?? value.code ?? 'Unbekannter Fehler')
}

async function load(silent = false): Promise<void> {
  const workspaceId = workspaceStore.activeId
  const runId = typeof route.params.id === 'string' ? route.params.id : ''
  if (!workspaceId || !runId) return
  if (!silent) loading.value = true
  try {
    const [loadedRun, loadedActions] = await Promise.all([
      api.get<SyncRun>(`/workspaces/${workspaceId}/runs/${runId}`, { workspaceId, cache: 'no-store' }),
      api.get<SyncActionPage>(`/workspaces/${workspaceId}/runs/${runId}/actions?limit=${actionLimit}&offset=${actionOffset.value}`, { workspaceId, cache: 'no-store' }),
    ])
    run.value = loadedRun
    actions.value = loadedActions
    error.value = null
    if (active.value) schedulePoll()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

function changeActionPage(direction: -1 | 1): void {
  if (!actions.value) return
  actionOffset.value = Math.max(0, actionOffset.value + direction * actionLimit)
  void load()
}

function schedulePoll(): void {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = window.setTimeout(() => void load(true), 3_000)
}

async function cancel(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId || !run.value) return
  try {
    run.value = await api.post<SyncRun>(`/workspaces/${workspaceId}/runs/${run.value.id}/cancel`)
    toasts.show('success', 'Lauf wurde abgebrochen')
  } catch (cause) {
    toasts.show('error', 'Lauf konnte nicht abgebrochen werden', errorMessage(cause))
  }
}

onMounted(load)
onBeforeUnmount(() => { if (pollTimer) window.clearTimeout(pollTimer) })
</script>

<template>
  <PageHeader :title="run ? `Lauf ${run.id.slice(0, 8)}` : 'Laufdetails'" eyebrow="Sync-Historie" description="Plan, Anwendung und Verifikation werden getrennt ausgewiesen.">
    <RouterLink class="button button-secondary" to="/runs">Zur Historie</RouterLink>
    <button v-if="run?.status === 'queued' && workspaceStore.canOperate" class="button button-danger" type="button" @click="cancel">Abbrechen</button>
  </PageHeader>
  <LoadingState v-if="loading" />
  <ErrorBanner v-else-if="error" :message="error" />
  <template v-else-if="run">
    <section class="card run-hero" :class="`run-${run.status}`"><div><StatusBadge :status="run.status" /><h2>{{ run.dry_run ? 'Vorschau / Dry-run' : 'Synchronisationslauf' }}</h2><p v-if="active">Der Lauf wird automatisch aktualisiert. Du kannst diese Seite offen lassen.</p><p v-else-if="run.error">{{ errorDetail(run.error) }}</p><p v-else>Der Lauf wurde abgeschlossen.</p></div><dl><div><dt>Erstellt</dt><dd>{{ formatDateTime(run.created_at) }}</dd></div><div><dt>Dauer</dt><dd>{{ duration(run.started_at, run.finished_at) }}</dd></div><div><dt>Auslöser</dt><dd>{{ run.trigger }}</dd></div><div><dt>Konfiguration</dt><dd>Revision #{{ run.config_revision }}</dd></div></dl></section>
    <section class="metric-grid compact" aria-label="Aktionsstatus"><article class="metric-card"><div><small>Gesamt</small><strong>{{ actions?.total ?? 0 }}</strong></div></article><article class="metric-card"><div><small>Verifiziert</small><strong>{{ actionCounts?.verified ?? 0 }}</strong></div></article><article class="metric-card"><div><small>Übersprungen</small><strong>{{ actionCounts?.skipped ?? 0 }}</strong></div></article><article class="metric-card"><div><small>Fehler</small><strong>{{ actionCounts?.failed ?? 0 }}</strong></div></article></section>
    <section class="card actions-panel"><div class="section-heading"><div><h2>Aktionen</h2><p>Remote-Schritte in ihrer tatsächlichen Reihenfolge</p></div><button class="button button-small button-secondary" type="button" @click="load()">Aktualisieren</button></div>
      <div v-if="!actions?.items.length" class="inline-empty">{{ active ? 'Der Plan wird gerade erstellt …' : 'Dieser Lauf enthält keine Einzelaktionen.' }}</div>
      <ol v-else class="run-action-list"><li v-for="action in actions.items" :key="action.id" :class="`action-${action.status}`"><button type="button" :aria-expanded="expandedAction === action.id" @click="expandedAction = expandedAction === action.id ? null : action.id"><span class="action-index">{{ action.ordinal + 1 }}</span><span class="action-copy"><strong>{{ actionDescription(action) }}</strong><small>{{ action.kind }} · Event {{ action.event_id || '–' }}</small></span><span class="action-status">{{ action.status }}</span><span aria-hidden="true">⌄</span></button><div v-if="expandedAction === action.id" class="action-detail"><dl><div><dt>Quelle</dt><dd>{{ action.source_id || '–' }}</dd></div><div><dt>Ziel</dt><dd>{{ action.target_id || '–' }}</dd></div><div><dt>Geplant</dt><dd>{{ formatDateTime(action.planned_at) }}</dd></div><div><dt>Verifiziert</dt><dd>{{ formatDateTime(action.verified_at) }}</dd></div></dl><div v-if="action.error" class="alert alert-error"><strong>Fehler</strong><span>{{ errorDetail(action.error) }}</span></div><details><summary>Technische Nutzdaten</summary><pre>{{ JSON.stringify(action.payload, null, 2) }}</pre></details></div></li></ol>
      <nav v-if="actions?.total" class="pagination" aria-label="Aktionsseiten"><button class="button button-small button-secondary" type="button" :disabled="actions.offset === 0" @click="changeActionPage(-1)">Zurück</button><span>{{ actionPageStart }}–{{ actionPageEnd }} von {{ actions.total }}</span><button class="button button-small button-secondary" type="button" :disabled="actions.offset + actions.items.length >= actions.total" @click="changeActionPage(1)">Weiter</button></nav>
    </section>
    <section v-if="run.plan" class="card technical-panel"><details><summary>Persistierten Plan anzeigen</summary><pre>{{ JSON.stringify(run.plan, null, 2) }}</pre></details></section>
  </template>
</template>
