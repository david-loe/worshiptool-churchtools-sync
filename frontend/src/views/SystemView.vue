<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useFocusTrap } from '@/composables/useFocusTrap'
import { api, ApiError, errorMessage } from '@/api/client'
import type { AdminWorkspace, Page, SystemStatus } from '@/api/types'
import { formatDateTime } from '@/utils/format'
import { useToastStore } from '@/stores/toasts'

const toasts = useToastStore()
const status = ref<SystemStatus | null>(null)
const workspaces = ref<AdminWorkspace[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const healthError = ref<string | null>(null)
const mfaRequired = ref(false)
const search = ref('')
const offset = ref(0)
const total = ref(0)
const limit = 50
const editing = ref<AdminWorkspace | null>(null)
const profileQuota = ref(3)
const memberQuota = ref(10)
const saving = ref(false)
const quotaDialog = ref<HTMLElement | null>(null)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / limit)))
const currentPage = computed(() => Math.floor(offset.value / limit) + 1)

useFocusTrap(quotaDialog, () => Boolean(editing.value), {
  onEscape: () => { editing.value = null },
  initialFocus: (container) => container.querySelector<HTMLElement>('input:not([disabled])'),
})

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  healthError.value = null
  mfaRequired.value = false
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset.value) })
  if (search.value.trim()) query.set('search', search.value.trim())
  const [healthResult, workspaceResult] = await Promise.allSettled([
    loadHealth(),
    api.get<Page<AdminWorkspace>>(`/admin/workspaces?${query}`, { cache: 'no-store' }),
  ])
  if (healthResult.status === 'fulfilled') status.value = healthResult.value
  else {
    status.value = null
    healthError.value = 'Der technische Readiness-Status konnte nicht geladen werden.'
  }
  if (workspaceResult.status === 'fulfilled') {
    workspaces.value = workspaceResult.value.items
    total.value = workspaceResult.value.total
  } else {
    const cause = workspaceResult.reason as unknown
    if (cause instanceof ApiError && (cause.hasCode('platform_admin_mfa_required') || cause.hasCode('platform_admin_mfa_stale'))) {
      mfaRequired.value = true
      error.value = 'Diese Administrationsseite benötigt eine aktuelle, beim Login mit TOTP bestätigte Sitzung. Melde dich ab und erneut mit deinem zweiten Faktor an.'
    } else if (cause instanceof ApiError && cause.status === 403) {
      error.value = 'Dein Konto ist für die Plattform-Administration nicht berechtigt.'
    } else {
      error.value = errorMessage(cause)
    }
  }
  loading.value = false
}

async function loadHealth(): Promise<SystemStatus> {
  const response = await fetch('/health/ready', {
    headers: { Accept: 'application/json, application/problem+json' },
    credentials: 'same-origin',
    cache: 'no-store',
  })
  if (!response.ok && response.status !== 503) {
    throw new Error(`Healthcheck HTTP ${response.status}`)
  }
  return response.json() as Promise<SystemStatus>
}

function applySearch(): void {
  offset.value = 0
  void load()
}

function edit(workspace: AdminWorkspace): void {
  editing.value = workspace
  profileQuota.value = workspace.profile_quota
  memberQuota.value = workspace.member_quota
}

async function saveQuotas(): Promise<void> {
  if (!editing.value) return
  saving.value = true
  try {
    const updated = await api.patch<AdminWorkspace>(`/admin/workspaces/${editing.value.id}/quotas`, {
      profile_quota: profileQuota.value,
      member_quota: memberQuota.value,
    })
    const index = workspaces.value.findIndex((item) => item.id === updated.id)
    if (index >= 0) workspaces.value[index] = updated
    editing.value = null
    toasts.show('success', 'Workspace-Quoten aktualisiert', 'Die Änderung wurde im Audit-Log protokolliert.')
  } catch (cause) {
    toasts.show('error', 'Quoten konnten nicht geändert werden', errorMessage(cause))
  } finally {
    saving.value = false
  }
}

function changePage(direction: -1 | 1): void {
  offset.value = Math.max(0, offset.value + direction * limit)
  void load()
}

onMounted(load)
</script>

<template>
  <PageHeader title="Systemverwaltung" eyebrow="Plattform" description="Technischer Zustand und Quoten aller Workspaces auf dieser Instanz."><button class="button button-secondary" type="button" @click="load">Aktualisieren</button></PageHeader>
  <LoadingState v-if="loading" />
  <template v-else>
    <ErrorBanner v-if="error" :message="error" />
    <div v-if="healthError" class="alert alert-warning"><strong>Readiness nicht verfügbar</strong><span>{{ healthError }}</span></div>
    <div v-if="mfaRequired" class="alert alert-warning"><strong>Erneute Anmeldung erforderlich</strong><span>TOTP wird für jede Plattform-Admin-Sitzung verpflichtend geprüft.</span></div>
    <section v-if="status" class="metric-grid compact"><article class="metric-card"><div><small>API-Version</small><strong>{{ status.version }}</strong><StatusBadge :status="status.status === 'ok' ? 'healthy' : 'degraded'" /></div></article><article class="metric-card"><div><small>Datenbank</small><strong>{{ status.database }}</strong><StatusBadge :status="status.database === 'ok' ? 'healthy' : 'error'" /></div></article><article class="metric-card"><div><small>Redis / Queue</small><strong>{{ status.redis }}</strong><StatusBadge :status="status.redis === 'ok' ? 'healthy' : 'error'" /></div></article><article class="metric-card"><div><small>Workspaces</small><strong>{{ total }}</strong><span>mandantenfähig isoliert</span></div></article></section>
    <section v-if="!error" class="card admin-workspaces"><div class="section-heading"><div><h2>Workspace-Quoten</h2><p>Profil- und Mitgliederlimits pro Mandant</p></div><form class="admin-search" @submit.prevent="applySearch"><label><span class="sr-only">Workspaces suchen</span><input v-model="search" type="search" placeholder="Name oder Slug suchen" /></label><button class="button button-small button-secondary" type="submit">Suchen</button></form></div>
      <div class="table-card flat"><table><thead><tr><th>Workspace</th><th>Profile</th><th>Mitglieder</th><th>Erstellt</th><th><span class="sr-only">Aktion</span></th></tr></thead><tbody><tr v-for="workspace in workspaces" :key="workspace.id"><td data-label="Workspace"><strong>{{ workspace.name }}</strong><small class="table-sub">{{ workspace.slug }}<template v-if="workspace.archived_at"> · archiviert</template></small></td><td data-label="Profile">{{ workspace.profile_count }} / {{ workspace.profile_quota }}</td><td data-label="Mitglieder">{{ workspace.member_count }} / {{ workspace.member_quota }}</td><td data-label="Erstellt">{{ formatDateTime(workspace.created_at) }}</td><td class="workspace-quota-action"><button class="button button-small button-secondary" type="button" @click="edit(workspace)">Quoten ändern</button></td></tr><tr v-if="!workspaces.length"><td colspan="5">Keine Workspaces gefunden.</td></tr></tbody></table></div>
      <nav class="pagination" aria-label="Workspace-Seiten"><button class="button button-small button-secondary" type="button" :disabled="offset === 0" @click="changePage(-1)">Zurück</button><span>Seite {{ currentPage }} von {{ totalPages }}</span><button class="button button-small button-secondary" type="button" :disabled="offset + limit >= total" @click="changePage(1)">Weiter</button></nav>
    </section>
  </template>

  <div v-if="editing" class="dialog-backdrop" @click.self="editing = null"><section ref="quotaDialog" class="dialog quota-dialog" role="dialog" aria-modal="true" aria-labelledby="quota-title" tabindex="-1"><header><div><p class="eyebrow">Plattform-Administration</p><h2 id="quota-title">Quoten für {{ editing.name }}</h2></div><button class="icon-button" type="button" aria-label="Dialog schließen" @click="editing = null">×</button></header><form class="stack-form" @submit.prevent="saveQuotas"><label><span>Maximale Sync-Profile</span><input v-model.number="profileQuota" type="number" min="1" max="1000" required /><small>Aktuell verwendet: {{ editing.profile_count }}</small></label><label><span>Maximale Mitglieder</span><input v-model.number="memberQuota" type="number" min="1" max="10000" required /><small>Aktuell verwendet: {{ editing.member_count }}</small></label><div class="alert alert-warning"><strong>Plattformweite Änderung</strong><span>Diese Aktion wird mit deinem Konto im Audit-Log gespeichert.</span></div><div class="dialog-actions"><button class="button button-secondary" type="button" @click="editing = null">Abbrechen</button><button class="button button-primary" type="submit" :disabled="saving">{{ saving ? 'Speichert …' : 'Quoten speichern' }}</button></div></form></section></div>
</template>
