<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import EmptyState from '@/components/EmptyState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, errorMessage } from '@/api/client'
import type { Page, RunStatus, SyncProfile, SyncRun } from '@/api/types'
import { duration, formatDateTime } from '@/utils/format'
import { useWorkspaceStore } from '@/stores/workspaces'

const workspaceStore = useWorkspaceStore()
const runs = ref<SyncRun[]>([])
const profiles = ref<SyncProfile[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const statusFilter = ref<RunStatus | ''>('')
const profileFilter = ref('')
const offset = ref(0)
const limit = 25
const total = ref(0)
const pageNumber = computed(() => Math.floor(offset.value / limit) + 1)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / limit)))
const profileName = (id: string): string => profiles.value.find((item) => item.id === id)?.name ?? id.slice(0, 8)

async function load(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  loading.value = true
  error.value = null
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset.value) })
  if (statusFilter.value) query.set('status', statusFilter.value)
  if (profileFilter.value) query.set('profile_id', profileFilter.value)
  try {
    const [runPage, profilePage] = await Promise.all([
      api.get<Page<SyncRun>>(`/workspaces/${workspaceId}/runs?${query}`, { workspaceId }),
      profiles.value.length ? Promise.resolve(profiles.value) : api.allPages<SyncProfile>(`/workspaces/${workspaceId}/profiles`, { workspaceId, cacheForMs: 30_000 }),
    ])
    runs.value = runPage.items
    total.value = runPage.total
    profiles.value = profilePage
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

function applyFilters(): void {
  offset.value = 0
  void load()
}

watch(offset, load)
onMounted(load)
</script>

<template>
  <PageHeader title="Sync-Historie" eyebrow="Nachvollziehbarkeit" description="Jeder Lauf und jede geplante oder ausgeführte Aktion bleibt transparent." />
  <section class="filter-bar" aria-label="Historie filtern">
    <label><span class="sr-only">Profil</span><select v-model="profileFilter" @change="applyFilters"><option value="">Alle Profile</option><option v-for="profile in profiles" :key="profile.id" :value="profile.id">{{ profile.name }}</option></select></label>
    <label><span class="sr-only">Status</span><select v-model="statusFilter" @change="applyFilters"><option value="">Alle Status</option><option value="succeeded">Erfolgreich</option><option value="partial">Teilweise</option><option value="failed">Fehlgeschlagen</option><option value="running">Läuft</option><option value="queued">Eingereiht</option><option value="canceled">Abgebrochen</option><option value="skipped">Übersprungen</option></select></label>
    <button class="button button-small button-secondary" type="button" @click="load">Aktualisieren</button>
  </section>
  <LoadingState v-if="loading" />
  <ErrorBanner v-else-if="error" :message="error" />
  <EmptyState v-else-if="!runs.length" title="Keine Läufe gefunden" text="Passe die Filter an oder starte einen Sync über ein aktives Profil." symbol="◷" />
  <template v-else>
    <div class="table-card">
      <table>
        <thead><tr><th>Status</th><th>Profil</th><th>Auslöser</th><th>Gestartet</th><th>Dauer</th><th>Revision</th><th><span class="sr-only">Details</span></th></tr></thead>
        <tbody><tr v-for="run in runs" :key="run.id"><td><StatusBadge :status="run.status" /></td><td><strong>{{ profileName(run.profile_id) }}</strong><small class="table-sub">{{ run.dry_run ? 'Dry-run' : 'Produktiv' }}</small></td><td>{{ run.trigger === 'manual' ? 'Manuell' : run.trigger === 'scheduled' ? 'Zeitplan' : 'Wiederherstellung' }}</td><td>{{ formatDateTime(run.started_at || run.created_at) }}</td><td>{{ duration(run.started_at, run.finished_at) }}</td><td>#{{ run.config_revision }}</td><td><RouterLink class="row-link" :to="`/runs/${run.id}`" :aria-label="`Details zu Lauf ${run.id}`">→</RouterLink></td></tr></tbody>
      </table>
    </div>
    <nav class="pagination" aria-label="Seitennavigation"><button class="button button-small button-secondary" type="button" :disabled="offset === 0" @click="offset = Math.max(0, offset - limit)">Zurück</button><span>Seite {{ pageNumber }} von {{ totalPages }}</span><button class="button button-small button-secondary" type="button" :disabled="offset + limit >= total" @click="offset += limit">Weiter</button></nav>
  </template>
</template>
