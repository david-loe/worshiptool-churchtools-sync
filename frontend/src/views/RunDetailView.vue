<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, errorMessage } from '@/api/client'
import type { RunEventResult, RunEventStatus, SyncAction, SyncActionPage, SyncRun, SyncRunResult } from '@/api/types'
import { groupRunEvents, newSongShareText, runEventFromQuery } from '@/domain/run'
import { duration, formatDateTime } from '@/utils/format'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useToastStore } from '@/stores/toasts'

const route = useRoute()
const workspaceStore = useWorkspaceStore()
const toasts = useToastStore()
const run = ref<SyncRun | null>(null)
const result = ref<SyncRunResult | null>(null)
const eventActions = ref<Record<string, SyncActionPage>>({})
const eventActionOffsets = ref<Record<string, number>>({})
const preparationActions = ref<SyncActionPage | null>(null)
const preparationOffset = ref(0)
const expandedGroups = ref<Set<RunEventStatus>>(new Set())
const expandedEvents = ref<Set<string>>(new Set())
const expandedActions = ref<Set<string>>(new Set())
const loadingActions = ref<Set<string>>(new Set())
const loading = ref(true)
const error = ref<string | null>(null)
const actionLimit = 25
let pollTimer: number | undefined
let expansionInitialized = false
let deepLinkHandled = false

const active = computed(() => run.value?.status === 'queued' || run.value?.status === 'running')
const canShare = computed(() => typeof navigator !== 'undefined' && typeof navigator.share === 'function')
const groups: Array<{ status: RunEventStatus; label: string }> = [
  { status: 'failed', label: 'Fehler' },
  { status: 'skipped', label: 'Übersprungen' },
  { status: 'planned', label: 'Geplant/offen' },
  { status: 'verified', label: 'Verifiziert' },
]

function eventsFor(status: RunEventStatus): RunEventResult[] {
  return result.value ? groupRunEvents(result.value.events)[status] : []
}

function groupCount(status: RunEventStatus): number {
  return result.value?.[status] ?? 0
}

function eventStatusLabel(status: RunEventStatus): string {
  return {
    failed: 'Fehler',
    skipped: 'Übersprungen',
    planned: 'Geplant/offen',
    verified: 'Verifiziert',
  }[status]
}

function eventName(event: RunEventResult): string {
  return event.target_event_name || event.source_event_name || event.target_event_id || event.source_event_id || 'Laufergebnis'
}

function eventStart(event: RunEventResult): string | null {
  return event.target_event_starts_at || event.source_event_starts_at?.[0] || null
}

function eventDomId(eventId: string): string {
  return `event-result-${eventId.replace(/[^A-Za-z0-9_-]/g, '-')}`
}

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
    const [loadedRun, loadedResult] = await Promise.all([
      api.get<SyncRun>(`/workspaces/${workspaceId}/runs/${runId}`, { workspaceId, cache: 'no-store' }),
      api.get<SyncRunResult>(`/workspaces/${workspaceId}/runs/${runId}/result`, { workspaceId, cache: 'no-store' }),
    ])
    run.value = loadedRun
    result.value = loadedResult
    error.value = null
    initializeExpansion()
    if (active.value) schedulePoll()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

function initializeExpansion(): void {
  if (!run.value || !result.value) return
  const event = runEventFromQuery(result.value.events, route.query.event)
  if (event && !deepLinkHandled) {
    expandedGroups.value = new Set([...expandedGroups.value, event.status])
    expandedEvents.value = new Set([...expandedEvents.value, event.id])
    deepLinkHandled = true
    void nextTick(() => document.getElementById(eventDomId(event.id))?.focus({ preventScroll: false }))
  } else if (
    !expansionInitialized
    && expandedGroups.value.size === 0
    && result.value.failed > 0
    && ['failed', 'partial'].includes(run.value.status)
  ) {
    expandedGroups.value = new Set(['failed'])
  }
  expansionInitialized = true
  for (const expandedEventId of expandedEvents.value) {
    const expandedEvent = result.value.events.find((item) => item.id === expandedEventId)
    if (expandedEvent) void loadEventActions(expandedEvent)
  }
}

function toggleGroup(status: RunEventStatus, open: boolean): void {
  const next = new Set(expandedGroups.value)
  if (open) next.add(status)
  else next.delete(status)
  expandedGroups.value = next
}

function toggleEvent(event: RunEventResult, open: boolean): void {
  const next = new Set(expandedEvents.value)
  if (open) {
    next.add(event.id)
    void loadEventActions(event)
  } else next.delete(event.id)
  expandedEvents.value = next
}

async function loadEventActions(event: RunEventResult, offset = eventActionOffsets.value[event.id] ?? 0): Promise<void> {
  if (!event.action_total || event.id.startsWith('run:')) return
  const workspaceId = workspaceStore.activeId
  if (!workspaceId || !run.value) return
  loadingActions.value = new Set([...loadingActions.value, event.id])
  try {
    const page = await api.get<SyncActionPage>(
      `/workspaces/${workspaceId}/runs/${run.value.id}/result/events/${encodeURIComponent(event.id)}/actions?limit=${actionLimit}&offset=${offset}`,
      { workspaceId, cache: 'no-store' },
    )
    eventActions.value = { ...eventActions.value, [event.id]: page }
    eventActionOffsets.value = { ...eventActionOffsets.value, [event.id]: offset }
  } catch (cause) {
    toasts.show('error', 'Ereignisaktionen konnten nicht geladen werden', errorMessage(cause))
  } finally {
    const next = new Set(loadingActions.value)
    next.delete(event.id)
    loadingActions.value = next
  }
}

async function loadPreparation(offset = preparationOffset.value): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId || !run.value || !result.value?.preparation_action_total) return
  try {
    preparationActions.value = await api.get<SyncActionPage>(
      `/workspaces/${workspaceId}/runs/${run.value.id}/result/preparation-actions?limit=${actionLimit}&offset=${offset}`,
      { workspaceId, cache: 'no-store' },
    )
    preparationOffset.value = offset
  } catch (cause) {
    toasts.show('error', 'Vorbereitungsschritte konnten nicht geladen werden', errorMessage(cause))
  }
}

function toggleAction(actionId: string): void {
  const next = new Set(expandedActions.value)
  if (next.has(actionId)) next.delete(actionId)
  else next.add(actionId)
  expandedActions.value = next
}

async function copySong(event: RunEventResult, songIndex: number): Promise<void> {
  const startsAt = eventStart(event)
  const song = event.new_songs[songIndex]
  if (!startsAt || !song) return
  if (!navigator.clipboard?.writeText) {
    toasts.show('warning', 'Automatisches Kopieren wird nicht unterstützt', 'Der Text bleibt markierbar.')
    return
  }
  try {
    await navigator.clipboard.writeText(newSongShareText(song, startsAt))
    toasts.show('success', 'Songtext kopiert')
  } catch (cause) {
    toasts.show('error', 'Songtext konnte nicht kopiert werden', errorMessage(cause))
  }
}

async function shareSong(event: RunEventResult, songIndex: number): Promise<void> {
  const startsAt = eventStart(event)
  const song = event.new_songs[songIndex]
  if (!startsAt || !song || !navigator.share) return
  try {
    await navigator.share({ text: newSongShareText(song, startsAt) })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') return
    toasts.show('error', 'Songtext konnte nicht geteilt werden', errorMessage(cause))
  }
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
    await load(true)
    toasts.show('success', 'Lauf wurde abgebrochen')
  } catch (cause) {
    toasts.show('error', 'Lauf konnte nicht abgebrochen werden', errorMessage(cause))
  }
}

onMounted(load)
onBeforeUnmount(() => { if (pollTimer) window.clearTimeout(pollTimer) })
</script>

<template>
  <PageHeader :title="run ? `Lauf ${run.id.slice(0, 8)}` : 'Laufdetails'" eyebrow="Sync-Historie" description="Ergebnisse sind nach Status und Ereignis geordnet.">
    <RouterLink class="button button-secondary" to="/runs">Zur Historie</RouterLink>
    <button v-if="run?.status === 'queued' && workspaceStore.canOperate" class="button button-danger" type="button" @click="cancel">Abbrechen</button>
  </PageHeader>
  <LoadingState v-if="loading" />
  <ErrorBanner v-else-if="error" :message="error" />
  <template v-else-if="run && result">
    <section class="card run-hero" :class="`run-${run.status}`"><div><StatusBadge :status="run.status" /><h2>{{ run.dry_run ? 'Vorschau / Dry-run' : 'Synchronisationslauf' }}</h2><p v-if="active">Der Lauf wird automatisch aktualisiert. Du kannst diese Seite offen lassen.</p><p v-else-if="run.error">{{ errorDetail(run.error) }}</p><p v-else>Der Lauf wurde abgeschlossen.</p></div><dl><div><dt>Erstellt</dt><dd>{{ formatDateTime(run.created_at) }}</dd></div><div><dt>Dauer</dt><dd>{{ duration(run.started_at, run.finished_at) }}</dd></div><div><dt>Auslöser</dt><dd>{{ run.trigger }}</dd></div><div><dt>Konfiguration</dt><dd>Revision #{{ run.config_revision }}</dd></div></dl></section>
    <section class="metric-grid compact" aria-label="Ereignisstatus"><article class="metric-card"><div><small>Gesamt</small><strong>{{ result.total }}</strong></div></article><article class="metric-card"><div><small>Verifiziert</small><strong>{{ result.verified }}</strong></div></article><article class="metric-card"><div><small>Übersprungen</small><strong>{{ result.skipped }}</strong></div></article><article class="metric-card"><div><small>Fehler</small><strong>{{ result.failed }}</strong></div></article><article class="metric-card"><div><small>Geplant/offen</small><strong>{{ result.planned }}</strong></div></article></section>
    <section class="result-groups" aria-label="Ereignisergebnisse">
      <details v-for="group in groups" v-show="groupCount(group.status)" :key="group.status" class="card result-group" :open="expandedGroups.has(group.status)" @toggle="toggleGroup(group.status, ($event.currentTarget as HTMLDetailsElement).open)">
        <summary><span>{{ group.label }}</span><strong>{{ groupCount(group.status) }}</strong></summary>
        <div class="event-results">
          <details v-for="event in eventsFor(group.status)" :id="eventDomId(event.id)" :key="event.id" class="event-result" :open="expandedEvents.has(event.id)" tabindex="-1" @toggle="toggleEvent(event, ($event.currentTarget as HTMLDetailsElement).open)">
            <summary><span><strong>{{ eventName(event) }}</strong><small>{{ eventStart(event) ? formatDateTime(eventStart(event)) : 'Datum nicht im Legacy-Plan gespeichert' }}</small></span><span class="event-status">{{ eventStatusLabel(event.status) }}</span></summary>
            <div class="event-result-body">
              <ul v-if="event.messages.length" class="result-messages"><li v-for="(message, index) in event.messages" :key="`${message.code}-${index}`" :class="`message-${message.severity}`"><strong>{{ message.message }}</strong><small>{{ message.code }} · {{ message.phase === 'plan' ? 'Planung' : message.phase === 'execution' ? 'Ausführung' : 'Lauf' }}</small><details v-if="Object.keys(message.details).length"><summary>Technisches Detail</summary><pre>{{ JSON.stringify(message.details, null, 2) }}</pre></details></li></ul>
              <p v-else class="inline-empty">Für dieses Ereignis liegen keine zusätzlichen Meldungen vor.</p>
              <section v-if="event.new_songs.length && eventStart(event)" class="created-songs" aria-label="Neu erstellte Songs"><h3>{{ event.new_songs.length === 1 ? 'Neu erstellter Song' : 'Neu erstellte Songs' }}</h3><article v-for="(song, songIndex) in event.new_songs" :key="song.action_id" class="song-share-card"><textarea :value="newSongShareText(song, eventStart(event)!)" readonly rows="5" :aria-label="`Teilbarer Text für ${song.name}`"></textarea><div><button class="button button-small button-secondary" type="button" @click="copySong(event, songIndex)">Kopieren</button><button v-if="canShare" class="button button-small button-secondary" type="button" @click="shareSong(event, songIndex)">Teilen</button></div></article></section>
              <section class="event-actions" aria-label="Ereignisaktionen"><h3>Aktionen <span>{{ event.action_total }}</span></h3><p v-if="loadingActions.has(event.id)">Aktionen werden geladen …</p><p v-else-if="!event.action_total" class="inline-empty">Für dieses Ereignis waren keine Remote-Aktionen nötig.</p>
                <ol v-else-if="eventActions[event.id]" class="run-action-list"><li v-for="action in eventActions[event.id]!.items" :key="action.id" :class="`action-${action.status}`"><button type="button" :aria-expanded="expandedActions.has(action.id)" @click="toggleAction(action.id)"><span class="action-index">{{ action.ordinal + 1 }}</span><span class="action-copy"><strong>{{ actionDescription(action) }}</strong><small>{{ action.kind }}</small></span><span class="action-status">{{ action.status }}</span><span aria-hidden="true">⌄</span></button><div v-if="expandedActions.has(action.id)" class="action-detail"><dl><div><dt>Quelle</dt><dd>{{ action.source_id || '–' }}</dd></div><div><dt>Ziel</dt><dd>{{ action.target_id || '–' }}</dd></div><div><dt>Geplant</dt><dd>{{ formatDateTime(action.planned_at) }}</dd></div><div><dt>Verifiziert</dt><dd>{{ formatDateTime(action.verified_at) }}</dd></div></dl><div v-if="action.error" class="alert alert-error"><strong>Fehler</strong><span>{{ errorDetail(action.error) }}</span></div><details><summary>Technische Nutzdaten</summary><pre>{{ JSON.stringify(action.payload, null, 2) }}</pre></details></div></li></ol>
                <nav v-if="eventActions[event.id]?.total" class="pagination" aria-label="Aktionsseiten"><button class="button button-small button-secondary" type="button" :disabled="eventActions[event.id]!.offset === 0" @click="loadEventActions(event, Math.max(0, eventActions[event.id]!.offset - actionLimit))">Zurück</button><span>{{ eventActions[event.id]!.offset + 1 }}–{{ Math.min(eventActions[event.id]!.total, eventActions[event.id]!.offset + eventActions[event.id]!.items.length) }} von {{ eventActions[event.id]!.total }}</span><button class="button button-small button-secondary" type="button" :disabled="eventActions[event.id]!.offset + eventActions[event.id]!.items.length >= eventActions[event.id]!.total" @click="loadEventActions(event, eventActions[event.id]!.offset + actionLimit)">Weiter</button></nav>
              </section>
            </div>
          </details>
        </div>
      </details>
    </section>
    <section v-if="result.preparation_action_total" class="card preparation-panel"><details @toggle="($event.currentTarget as HTMLDetailsElement).open && loadPreparation()"><summary>Vorbereitung des Songkatalogs ({{ result.preparation_action_total }})</summary><ol v-if="preparationActions" class="run-action-list"><li v-for="action in preparationActions.items" :key="action.id" :class="`action-${action.status}`"><button type="button" :aria-expanded="expandedActions.has(action.id)" @click="toggleAction(action.id)"><span class="action-index">{{ action.ordinal + 1 }}</span><span class="action-copy"><strong>{{ actionDescription(action) }}</strong><small>{{ action.kind }}</small></span><span class="action-status">{{ action.status }}</span><span aria-hidden="true">⌄</span></button><div v-if="expandedActions.has(action.id)" class="action-detail"><div v-if="action.error" class="alert alert-error">{{ errorDetail(action.error) }}</div><details><summary>Technische Nutzdaten</summary><pre>{{ JSON.stringify(action.payload, null, 2) }}</pre></details></div></li></ol><nav v-if="preparationActions?.total" class="pagination" aria-label="Vorbereitungsseiten"><button class="button button-small button-secondary" type="button" :disabled="preparationActions.offset === 0" @click="loadPreparation(Math.max(0, preparationActions.offset - actionLimit))">Zurück</button><span>{{ preparationActions.offset + 1 }}–{{ Math.min(preparationActions.total, preparationActions.offset + preparationActions.items.length) }} von {{ preparationActions.total }}</span><button class="button button-small button-secondary" type="button" :disabled="preparationActions.offset + preparationActions.items.length >= preparationActions.total" @click="loadPreparation(preparationActions.offset + actionLimit)">Weiter</button></nav></details></section>
    <section v-if="run.plan" class="card technical-panel"><details><summary>Technisches JSON</summary><pre>{{ JSON.stringify(run.plan, null, 2) }}</pre></details></section>
  </template>
</template>

<style scoped>
.result-groups { display: grid; gap: .8rem; }
.metric-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.result-group > summary, .event-result > summary { cursor: pointer; display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 1rem; overflow-wrap: anywhere; }
.result-group > summary { padding: 1rem 1.25rem; font-size: 1.05rem; font-weight: 700; }
.result-group > summary strong { border-radius: 999px; min-width: 2rem; padding: .2rem .55rem; text-align: center; background: var(--surface-muted, #eef1f4); }
.result-group[open] > summary { border-bottom: 1px solid var(--line); }
.event-results { display: grid; gap: .65rem; margin-top: 1rem; padding: 0 1rem 1rem; }
.event-result { min-width: 0; overflow: hidden; border: 1px solid var(--border, #d9dee5); border-radius: .65rem; scroll-margin-top: 1rem; }
.event-result:focus { outline: 3px solid var(--focus, #4b7bec); outline-offset: 2px; }
.event-result > summary { padding: .85rem 1rem; }
.event-result > summary span:first-child { display: grid; min-width: 0; gap: .2rem; }
.event-result > summary small { font-weight: 400; }
.event-status { flex: 0 0 auto; font-size: .75rem; font-weight: 800; }
.event-result-body { display: grid; min-width: 0; gap: 1rem; padding: 1rem; border-top: 1px solid var(--line); }
.result-messages { display: grid; gap: .5rem; list-style: none; margin: 0; padding: 0; }
.result-messages > li { border-left: 4px solid #7b8794; display: grid; gap: .25rem; padding: .65rem .8rem; background: var(--surface-muted, #f5f7f9); }
.result-messages > .message-error { border-left-color: #b42318; }
.result-messages > .message-warning { border-left-color: #b7791f; }
.created-songs { display: grid; gap: .7rem; }
.song-share-card { display: grid; gap: .55rem; }
.song-share-card textarea { box-sizing: border-box; min-height: 8rem; resize: vertical; width: 100%; }
.song-share-card > div { display: flex; flex-wrap: wrap; gap: .5rem; }
.event-actions h3 span { font-weight: 400; }
@media (max-width: 1100px) { .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 640px) {
  .metric-grid {
    display: flex;
    grid-template-columns: none;
    margin-right: -.25rem;
    padding: 0 .25rem .55rem 0;
    overflow-x: auto;
    scroll-snap-type: x proximity;
    scrollbar-width: thin;
  }
  .metric-grid .metric-card {
    flex: 0 0 clamp(7.25rem, 30vw, 8.5rem);
    min-height: 88px;
    padding: .8rem;
    scroll-snap-align: start;
  }
  .metric-grid .metric-card strong { font-size: 1.3rem; }
  .event-result > summary { align-items: flex-start; }
  .event-results { margin-top: .8rem; padding: 0 .7rem .7rem; }
  .event-result-body { padding: .85rem; }
}
</style>
