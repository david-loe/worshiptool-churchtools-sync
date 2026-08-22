<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import StatusBadge from '@/components/StatusBadge.vue'
import type { ChurchToolsConnectionInput, Connection, ConnectionInput, Provider, ProviderMetadata, ProviderOption, SyncAction, SyncActionPage, SyncProfile, SyncProfileInput, SyncRun, WorshipToolsConnectionInput } from '@/api/types'
import { api, ApiError, errorMessage } from '@/api/client'
import { connectionPayload, connectionUpdatePayload } from '@/domain/connection'
import { connectionContinuation, profileContinuation } from '@/domain/onboarding'
import { newProfile, profileInputFromProfile, sanitizeProfile } from '@/domain/profile'
import { recoverableRunId } from '@/domain/run'
import { useWorkspaceStore } from '@/stores/workspaces'

const router = useRouter()
const workspaceStore = useWorkspaceStore()
const step = ref(workspaceStore.activeId ? 2 : 1)
const loading = ref(false)
const error = ref<string | null>(null)
const workspaceName = ref('')
const ctInput = ref<ChurchToolsConnectionInput>({ name: 'ChurchTools', provider: 'churchtools', base_url: '', credentials: { token: '' } })
const wtInput = ref<WorshipToolsConnectionInput>({ name: 'WorshipTools', provider: 'worshiptools', credentials: { email: '', password: '', account_id: '' } })
const ctConnection = ref<Connection | null>(null)
const wtConnection = ref<Connection | null>(null)
const ctTest = ref<ProviderTestResult | null>(null)
const wtTest = ref<ProviderTestResult | null>(null)
const profile = ref<SyncProfileInput>(newProfile())
const savedProfile = ref<SyncProfile | null>(null)
const preview = ref<SyncRun | null>(null)
const previewActions = ref<SyncAction[]>([])
const songCategories = ref<ProviderOption[]>([])
const steps = ['Workspace', 'Verbindungen', 'Sync-Profil', 'Prüfen']
const previewApproved = computed(() => preview.value?.status === 'succeeded' || preview.value?.status === 'partial')
let previewPollTimer: number | undefined

interface ProviderTestResult {
  succeeded: boolean
  message: string
}

watch(ctInput, () => { ctTest.value = null }, { deep: true })
watch(wtInput, () => { wtTest.value = null }, { deep: true })

async function createWorkspace(): Promise<void> {
  if (!workspaceName.value.trim()) return
  await run(async () => {
    await workspaceStore.create(workspaceName.value)
    step.value = 2
  })
}

async function createConnections(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  await run(async () => {
    const existing = await api.allPages<Connection>(`/workspaces/${workspaceId}/connections`, { workspaceId, cache: 'no-store' })
    const providers: Provider[] = []
    if (!ctTest.value?.succeeded) providers.push('churchtools')
    if (!wtTest.value?.succeeded) providers.push('worshiptools')
    const results = await Promise.allSettled(providers.map((provider) => connectAndTest(provider, existing)))
    const failures = results
      .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
      .map((result) => errorMessage(result.reason))
    if (failures.length || !ctTest.value?.succeeded || !wtTest.value?.succeeded) {
      throw new Error(failures.join(' ') || 'Mindestens eine Verbindung konnte nicht bestätigt werden.')
    }
    await continueAfterConnections()
  })
}

async function retryConnection(provider: Provider): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  await run(async () => {
    const validationError = providerInputError(provider)
    if (validationError) throw new Error(validationError)
    const existing = await api.allPages<Connection>(`/workspaces/${workspaceId}/connections`, { workspaceId, cache: 'no-store' })
    await connectAndTest(provider, existing)
    if (ctTest.value?.succeeded && wtTest.value?.succeeded) await continueAfterConnections()
  })
}

async function connectAndTest(provider: Provider, existing: Connection[]): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  const validationError = providerInputError(provider)
  if (validationError) throw new Error(validationError)
  const input: ConnectionInput = provider === 'churchtools' ? ctInput.value : wtInput.value
  try {
    const current = provider === 'churchtools' ? ctConnection.value : wtConnection.value
    const connection = await ensureConnection(workspaceId, input, current, existing)
    if (provider === 'churchtools') ctConnection.value = connection
    else wtConnection.value = connection
    const result = await api.post<ProviderTestResult>(`/workspaces/${workspaceId}/connections/${connection.id}/test`)
    if (provider === 'churchtools') ctTest.value = result
    else wtTest.value = result
    if (!result.succeeded) throw new Error(`${provider === 'churchtools' ? 'ChurchTools' : 'WorshipTools'}: ${result.message || 'Verbindungstest fehlgeschlagen.'}`)
  } catch (cause) {
    const failed = { succeeded: false, message: errorMessage(cause) }
    if (provider === 'churchtools') ctTest.value = failed
    else wtTest.value = failed
    throw cause
  }
}

async function ensureConnection(
  workspaceId: string,
  input: ConnectionInput,
  current: Connection | null,
  existing: Connection[],
): Promise<Connection> {
  let connection = current ?? connectionContinuation(existing, input)
  if (!connection) {
    try {
      return await api.post<Connection>(`/workspaces/${workspaceId}/connections`, connectionPayload(input, true), { workspaceId })
    } catch (cause) {
      if (!(cause instanceof ApiError) || cause.status !== 409) throw cause
      const refreshed = await api.allPages<Connection>(`/workspaces/${workspaceId}/connections`, { workspaceId, cache: 'no-store' })
      connection = connectionContinuation(refreshed, input)
      if (!connection) throw cause
    }
  }
  try {
    return await api.patch<Connection>(
      `/workspaces/${workspaceId}/connections/${connection.id}`,
      connectionUpdatePayload(input, true),
      { workspaceId, ifMatch: `"${connection.revision}"` },
    )
  } catch (cause) {
    if (!(cause instanceof ApiError) || cause.status !== 412) throw cause
    const refreshed = await api.allPages<Connection>(`/workspaces/${workspaceId}/connections`, { workspaceId, cache: 'no-store' })
    const latest = connectionContinuation(refreshed, input)
    if (!latest) throw cause
    return api.patch<Connection>(
      `/workspaces/${workspaceId}/connections/${latest.id}`,
      connectionUpdatePayload(input, true),
      { workspaceId, ifMatch: `"${latest.revision}"` },
    )
  }
}

async function continueAfterConnections(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  const churchtools = ctConnection.value
  const worshiptools = wtConnection.value
  if (!workspaceId || !churchtools || !worshiptools) return
  profile.value.source_connection_id = worshiptools.id
  profile.value.target_connection_id = churchtools.id
  try {
    const metadata = await api.get<ProviderMetadata>(`/workspaces/${workspaceId}/connections/${churchtools.id}/metadata`, { workspaceId })
    songCategories.value = metadata.data.song_categories
  } catch (cause) {
    if (!(cause instanceof ApiError) || ![501, 502].includes(cause.status)) throw cause
  }
  const profiles = await api.allPages<SyncProfile>(`/workspaces/${workspaceId}/profiles`, { workspaceId, cache: 'no-store' })
  const continuation = profileContinuation(profiles, profile.value)
  if (continuation) {
    savedProfile.value = continuation
    profile.value = profileInputFromProfile(continuation)
  }
  step.value = 3
}

function providerInputError(provider: Provider): string | null {
  if (provider === 'churchtools') {
    if (!ctInput.value.base_url.trim() || !ctInput.value.credentials?.token?.trim()) return 'ChurchTools-Adresse und Login-Token sind erforderlich.'
    return null
  }
  const credentials = wtInput.value.credentials
  return credentials?.email?.trim() && credentials.password && credentials.account_id?.trim()
    ? null
    : 'WorshipTools-E-Mail, Passwort und Account-ID sind erforderlich.'
}

async function createProfile(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  await run(async () => {
    const payload = sanitizeProfile(profile.value)
    const profiles = await api.allPages<SyncProfile>(`/workspaces/${workspaceId}/profiles`, { workspaceId, cache: 'no-store' })
    let existing = savedProfile.value ?? profileContinuation(profiles, payload)
    const conflictingName = profiles.find((item) => item.name === payload.name && item.id !== existing?.id)
    if (!existing && conflictingName) throw new Error('Ein anderes Profil verwendet diesen Namen bereits. Wähle einen eindeutigen Profilnamen.')
    if (existing) {
      try {
        savedProfile.value = await api.patch<SyncProfile>(
          `/workspaces/${workspaceId}/profiles/${existing.id}`,
          payload,
          { workspaceId, ifMatch: `"${existing.revision}"` },
        )
      } catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 412) throw cause
        const refreshed = await api.allPages<SyncProfile>(`/workspaces/${workspaceId}/profiles`, { workspaceId, cache: 'no-store' })
        existing = refreshed.find((item) => item.id === existing?.id)
        if (!existing) throw cause
        savedProfile.value = await api.patch<SyncProfile>(
          `/workspaces/${workspaceId}/profiles/${existing.id}`,
          payload,
          { workspaceId, ifMatch: `"${existing.revision}"` },
        )
      }
    } else {
      try {
        savedProfile.value = await api.post<SyncProfile>(`/workspaces/${workspaceId}/profiles`, payload, { workspaceId })
      } catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 409) throw cause
        const refreshed = await api.allPages<SyncProfile>(`/workspaces/${workspaceId}/profiles`, { workspaceId, cache: 'no-store' })
        existing = profileContinuation(refreshed, payload)
        if (!existing) throw cause
        savedProfile.value = await api.patch<SyncProfile>(
          `/workspaces/${workspaceId}/profiles/${existing.id}`,
          payload,
          { workspaceId, ifMatch: `"${existing.revision}"` },
        )
      }
    }
    step.value = 4
    preview.value = null
    previewActions.value = []
    await loadPreview()
  })
}

async function loadPreview(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId || !savedProfile.value) return
  if (previewPollTimer) window.clearTimeout(previewPollTimer)
  preview.value = null
  previewActions.value = []
  loading.value = true
  error.value = null
  try {
    try {
      preview.value = await api.post<SyncRun>(`/workspaces/${workspaceId}/profiles/${savedProfile.value.id}/preview`)
    } catch (cause) {
      if (!(cause instanceof ApiError) || cause.status !== 404) throw cause
      preview.value = await api.post<SyncRun>(`/workspaces/${workspaceId}/profiles/${savedProfile.value.id}/runs`, { dry_run: true })
    }
  } catch (cause) {
    const existingRunId = recoverableRunId(cause, workspaceId)
    if (existingRunId) {
      preview.value = await api.get<SyncRun>(`/workspaces/${workspaceId}/runs/${existingRunId}`, { workspaceId, cache: 'no-store' })
    } else {
      error.value = errorMessage(cause)
    }
  } finally {
    loading.value = false
  }
  await loadPreviewActions()
  if (preview.value?.status === 'queued' || preview.value?.status === 'running') schedulePreviewPoll()
}

async function loadPreviewActions(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId || !preview.value) return
  const page = await api.get<SyncActionPage>(
    `/workspaces/${workspaceId}/runs/${preview.value.id}/actions?limit=8&offset=0`,
    { workspaceId, cache: 'no-store' },
  )
  previewActions.value = page.items
}

function schedulePreviewPoll(): void {
  if (previewPollTimer) window.clearTimeout(previewPollTimer)
  previewPollTimer = window.setTimeout(() => void refreshPreview(), 2_500)
}

async function refreshPreview(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId || !preview.value) return
  try {
    preview.value = await api.get<SyncRun>(`/workspaces/${workspaceId}/runs/${preview.value.id}`, { workspaceId, cache: 'no-store' })
    await loadPreviewActions()
    if (preview.value.status === 'queued' || preview.value.status === 'running') schedulePreviewPoll()
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function finish(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId || !savedProfile.value) return
  const profileToEnable = savedProfile.value
  await run(async () => {
    await api.patch<SyncProfile>(`/workspaces/${workspaceId}/profiles/${profileToEnable.id}`, { enabled: true }, { ifMatch: `"${profileToEnable.revision}"` })
    await router.push('/dashboard')
  })
}

async function run(task: () => Promise<void>): Promise<void> {
  loading.value = true
  error.value = null
  try {
    await task()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

onBeforeUnmount(() => { if (previewPollTimer) window.clearTimeout(previewPollTimer) })
</script>

<template>
  <main class="onboarding-page">
    <header class="onboarding-header"><div class="brand"><span class="brand-mark">W</span><span><strong>WorshipTool</strong><small>Sync</small></span></div><button class="link-button" type="button" @click="$router.push('/dashboard')">Später fortsetzen</button></header>
    <section class="onboarding-card">
      <div class="step-progress" aria-label="Einrichtungsfortschritt">
        <div class="progress-track"><span :class="`step-${step}`" /></div>
        <ol><li v-for="(label, index) in steps" :key="label" :class="{ active: step === index + 1, done: step > index + 1 }"><span>{{ step > index + 1 ? '✓' : index + 1 }}</span>{{ label }}</li></ol>
      </div>

      <div v-if="error" class="alert alert-error" role="alert"><strong>Einrichtung unterbrochen</strong><span>{{ error }}</span></div>

      <form v-if="step === 1" class="wizard-content stack-form" @submit.prevent="createWorkspace">
        <div class="wizard-copy"><p class="eyebrow">Schritt 1 von 4</p><h1>Dein Workspace</h1><p>Hier verwaltest du später Profile, Verbindungen und Teammitglieder deiner Gemeinde.</p></div>
        <label><span>Name der Gemeinde / Organisation</span><input v-model="workspaceName" required autocomplete="organization" placeholder="z. B. FCG Musterstadt" /></label>
        <button class="button button-primary" type="submit" :disabled="loading">{{ loading ? 'Wird angelegt …' : 'Workspace anlegen' }}</button>
      </form>

      <form v-else-if="step === 2" class="wizard-content" @submit.prevent="createConnections">
        <div class="wizard-copy"><p class="eyebrow">Schritt 2 von 4</p><h1>Dienste verbinden</h1><p>Zugangsdaten werden verschlüsselt gespeichert und nach dem Speichern nie wieder angezeigt.</p></div>
        <div class="connection-setup-grid">
          <fieldset class="provider-card"><legend><span class="provider-logo ct">CT</span><span>ChurchTools<small>Quelle für Kalender und Ziel für Agenda</small></span></legend>
            <label><span>ChurchTools-Adresse</span><input v-model="ctInput.base_url" type="url" required placeholder="https://gemeinde.church.tools" /></label>
            <label><span>Login-Token</span><input v-model="ctInput.credentials!.token" type="password" autocomplete="off" required /><small>Wird nur verschlüsselt übertragen und gespeichert.</small></label>
            <div v-if="ctTest" class="alert" :class="ctTest.succeeded ? 'alert-success' : 'alert-error'" :role="ctTest.succeeded ? 'status' : 'alert'">
              <strong>{{ ctTest.succeeded ? 'ChurchTools verbunden' : 'ChurchTools konnte nicht verbunden werden' }}</strong>
              <span>{{ ctTest.message }}</span>
              <button v-if="!ctTest.succeeded" class="button button-small button-secondary" type="button" :disabled="loading" @click="retryConnection('churchtools')">Nur ChurchTools erneut prüfen</button>
            </div>
          </fieldset>
          <fieldset class="provider-card"><legend><span class="provider-logo wt">WT</span><span>WorshipTools<small>Quelle für Setlists</small></span></legend>
            <label><span>E-Mail-Adresse</span><input v-model="wtInput.credentials!.email" type="email" autocomplete="off" required /></label>
            <label><span>Passwort</span><input v-model="wtInput.credentials!.password" type="password" autocomplete="off" required /></label>
            <label><span>WorshipTools Account-ID</span><input v-model="wtInput.credentials!.account_id" autocomplete="off" required /></label>
            <div v-if="wtTest" class="alert" :class="wtTest.succeeded ? 'alert-success' : 'alert-error'" :role="wtTest.succeeded ? 'status' : 'alert'">
              <strong>{{ wtTest.succeeded ? 'WorshipTools verbunden' : 'WorshipTools konnte nicht verbunden werden' }}</strong>
              <span>{{ wtTest.message }}</span>
              <button v-if="!wtTest.succeeded" class="button button-small button-secondary" type="button" :disabled="loading" @click="retryConnection('worshiptools')">Nur WorshipTools erneut prüfen</button>
            </div>
          </fieldset>
        </div>
        <div class="wizard-actions"><button class="button button-secondary" type="button" @click="step = 1">Zurück</button><button class="button button-primary" type="submit" :disabled="loading">{{ loading ? 'Verbindungen werden geprüft …' : 'Verbinden und weiter' }}</button></div>
      </form>

      <form v-else-if="step === 3" class="wizard-content" @submit.prevent="createProfile">
        <div class="wizard-copy"><p class="eyebrow">Schritt 3 von 4</p><h1>Erstes Sync-Profil</h1><p>Diese sicheren Standardwerte kannst du später jederzeit anpassen.</p></div>
        <div class="form-grid">
          <label class="span-2"><span>Profilname</span><input v-model="profile.name" required /></label>
          <label><span>Vorausschau</span><div class="input-suffix"><input v-model.number="profile.lookahead_days" type="number" min="1" max="90" required /><span>Tage</span></div></label>
          <label><span>Intervall</span><select v-model.number="profile.interval_minutes"><option :value="30">30 Minuten</option><option :value="60">1 Stunde</option><option :value="120">2 Stunden</option><option :value="360">6 Stunden</option></select></label>
          <label class="span-2 check-card"><input v-model="profile.create_missing_songs" type="checkbox" /><span><strong>Fehlende Songs automatisch anlegen</strong><small>Neue Songs werden in ChurchTools mit einem Standard-Arrangement angelegt.</small></span></label>
          <label v-if="profile.create_missing_songs"><span>ChurchTools-Songkategorie</span><select v-if="songCategories.length" v-model.number="profile.song_category_id" required><option :value="null" disabled>Auswählen …</option><option v-for="category in songCategories" :key="category.id" :value="Number(category.id)">{{ category.name }}</option></select><input v-else v-model.number="profile.song_category_id" type="number" min="1" required placeholder="Kategorie-ID" /></label>
          <label v-if="profile.create_missing_songs"><span>Arrangement-Name</span><input v-model="profile.arrangement_name" required maxlength="50" /></label>
          <label class="span-2 check-card"><input v-model="profile.notification_preferences.email" type="checkbox" /><span><strong>E-Mail bei Problemen</strong><small>Erfolge werden standardmäßig nicht gemeldet.</small></span></label>
        </div>
        <div class="wizard-actions"><button class="button button-secondary" type="button" @click="step = 2">Zurück</button><button class="button button-primary" type="submit" :disabled="loading">{{ loading ? 'Profil wird gespeichert …' : 'Speichern und Vorschau' }}</button></div>
      </form>

      <section v-else class="wizard-content">
        <div class="wizard-copy"><p class="eyebrow">Schritt 4 von 4</p><h1>Bereit für den ersten Sync</h1><p>Die Vorschau verändert keine Daten. Prüfe die geplanten Aktionen, bevor das Profil aktiviert wird.</p></div>
        <div v-if="loading" class="loading-state"><span class="spinner" />Vorschau wird erstellt …</div>
        <div v-else-if="preview" class="preview-box">
          <div class="preview-summary"><strong>{{ previewApproved ? 'Vorschau abgeschlossen' : preview.status === 'failed' || preview.status === 'skipped' ? 'Vorschau nicht erfolgreich' : 'Vorschau wird berechnet' }}</strong><StatusBadge :status="preview.status" /></div>
          <ol v-if="previewActions.length" class="action-list"><li v-for="action in previewActions" :key="action.id"><span>{{ action.kind }}</span><strong>{{ String(action.payload.description || action.kind) }}</strong></li></ol>
          <p v-if="preview.status === 'failed' || preview.status === 'skipped'" class="danger-text">Das Profil bleibt deaktiviert. Öffne die Historie für Details oder gehe zurück und korrigiere die Konfiguration.</p><p v-else-if="!previewActions.length">Die Vorschau verändert keine Remotedaten. Details erscheinen nach der Planung in der Historie.</p>
        </div>
        <div class="wizard-actions"><button class="button button-secondary" type="button" @click="step = 3">Zurück</button><button v-if="preview?.status === 'failed' || preview?.status === 'skipped' || !preview" class="button button-secondary" type="button" :disabled="loading" @click="loadPreview">Vorschau erneut starten</button><button class="button button-primary" type="button" :disabled="loading || !previewApproved" @click="finish">Profil aktivieren</button></div>
      </section>
    </section>
  </main>
</template>
