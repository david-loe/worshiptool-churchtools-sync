<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import { api, ApiError, errorMessage } from '@/api/client'
import type { Connection, ProviderMetadata, ProviderOption, SyncProfile, SyncProfileInput, SyncRun } from '@/api/types'
import { newProfile, profileInputFromProfile, sanitizeProfile } from '@/domain/profile'
import { recoverableRunId } from '@/domain/run'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useToastStore } from '@/stores/toasts'

const route = useRoute()
const router = useRouter()
const workspaceStore = useWorkspaceStore()
const toasts = useToastStore()
const profileId = computed(() => typeof route.params.id === 'string' ? route.params.id : null)
const isNew = computed(() => !profileId.value)
const form = ref<SyncProfileInput>(newProfile())
const connections = ref<Connection[]>([])
const calendars = ref<ProviderOption[]>([])
const campuses = ref<ProviderOption[]>([])
const songCategories = ref<ProviderOption[]>([])
const loading = ref(true)
const saving = ref(false)
const previewing = ref(false)
const dirty = ref(false)
const initialized = ref(false)
const error = ref<string | null>(null)
const etag = ref<string | undefined>()
const sourceConnections = computed(() => connections.value.filter((item) => item.provider === 'worshiptools'))
const targetConnections = computed(() => connections.value.filter((item) => item.provider === 'churchtools'))

watch(form, () => { if (initialized.value) dirty.value = true }, { deep: true })

async function load(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  loading.value = true
  error.value = null
  try {
    connections.value = await api.allPages<Connection>(`/workspaces/${workspaceId}/connections`, { workspaceId })
    if (profileId.value) {
      const response = await api.getWithMeta<SyncProfile>(`/workspaces/${workspaceId}/profiles/${profileId.value}`, { workspaceId })
      etag.value = response.etag ?? `"${response.data.revision}"`
      form.value = profileInputFromProfile(response.data)
    } else {
      form.value.source_connection_id = sourceConnections.value[0]?.id ?? ''
      form.value.target_connection_id = targetConnections.value[0]?.id ?? ''
    }
    initialized.value = true
    dirty.value = false
    await loadMetadata()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function loadMetadata(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  const targetId = form.value.target_connection_id
  if (!workspaceId || !targetId) return
  try {
    const response = await api.get<ProviderMetadata>(`/workspaces/${workspaceId}/connections/${targetId}/metadata`, { workspaceId, cacheForMs: 60_000 })
    calendars.value = response.data.calendars
    campuses.value = response.data.campuses
    songCategories.value = response.data.song_categories
  } catch (cause) {
    if (!(cause instanceof ApiError) || ![501, 502].includes(cause.status)) throw cause
    calendars.value = []
    campuses.value = []
    songCategories.value = []
  }
}

function addRule(): void {
  form.value.event_rules.push({ name_contains: '', name_regex: '', calendar_ids: [], campus_ids: [] })
}

function removeRule(index: number): void {
  if (form.value.event_rules.length > 1) form.value.event_rules.splice(index, 1)
}

function addPlacement(): void {
  form.value.placements.push({ id: `placement-${form.value.placements.length + 1}`, anchor: { item_type: 'header', title: '' }, relation: 'after', song_start: 0, song_end: null })
}

function removePlacement(index: number): void {
  if (form.value.placements.length > 1) form.value.placements.splice(index, 1)
}

async function save(stay = false): Promise<SyncProfile | null> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return null
  const validationError = validate()
  if (validationError) {
    error.value = validationError
    document.getElementById('main-content')?.focus()
    return null
  }
  saving.value = true
  error.value = null
  try {
    const payload = sanitizeProfile(form.value)
    let saved: SyncProfile
    if (profileId.value) {
      saved = await api.patch<SyncProfile>(`/workspaces/${workspaceId}/profiles/${profileId.value}`, payload, { ifMatch: etag.value })
    } else {
      saved = await api.post<SyncProfile>(`/workspaces/${workspaceId}/profiles`, payload)
    }
    dirty.value = false
    etag.value = `"${saved.revision}"`
    toasts.show('success', 'Profil gespeichert')
    if (!stay) await router.push('/profiles')
    else if (!profileId.value) await router.replace(`/profiles/${saved.id}`)
    return saved
  } catch (cause) {
    error.value = cause instanceof ApiError && cause.status === 412
      ? 'Dieses Profil wurde zwischenzeitlich geändert. Bitte lade die Seite neu und übertrage deine Änderung erneut.'
      : errorMessage(cause)
    return null
  } finally {
    saving.value = false
  }
}

function validate(): string | null {
  if (!form.value.source_connection_id || !form.value.target_connection_id) return 'Wähle eine WorshipTools-Quelle und ein ChurchTools-Ziel.'
  if (form.value.create_missing_songs && !form.value.song_category_id) return 'Wähle eine ChurchTools-Songkategorie für automatisch angelegte Songs.'
  if (form.value.placements.some((placement) => !placement.anchor.item_id?.trim() && !placement.anchor.item_type?.trim() && !placement.anchor.title?.trim())) return 'Jede Platzierung benötigt mindestens einen Anker aus Item-ID, Typ oder Titel.'
  const placementIds = form.value.placements.map((placement) => placement.id.trim())
  if (new Set(placementIds).size !== placementIds.length) return 'Placement-IDs müssen innerhalb eines Profils eindeutig sein.'
  return null
}

async function preview(): Promise<void> {
  previewing.value = true
  const saved = dirty.value || isNew.value ? await save(true) : null
  const id = saved?.id ?? profileId.value
  const workspaceId = workspaceStore.activeId
  if (!workspaceId || !id) {
    previewing.value = false
    return
  }
  try {
    const run = await api.post<SyncRun>(`/workspaces/${workspaceId}/profiles/${id}/preview`)
    await router.push(`/runs/${run.id}`)
  } catch (cause) {
    const existingRunId = recoverableRunId(cause, workspaceId)
    if (existingRunId) {
      toasts.show('warning', 'Vorschau ist bereits gespeichert', 'Der persistierte Lauf wird geöffnet und bei Bedarf erneut eingereiht.')
      await router.push(`/runs/${existingRunId}`)
    } else {
      toasts.show('error', 'Vorschau konnte nicht gestartet werden', errorMessage(cause))
    }
  } finally {
    previewing.value = false
  }
}

onBeforeRouteLeave(() => !dirty.value || confirm('Ungespeicherte Änderungen verwerfen?'))
onMounted(load)
</script>

<template>
  <PageHeader :title="isNew ? 'Neues Sync-Profil' : 'Sync-Profil bearbeiten'" eyebrow="Konfiguration" description="Regeln sind strukturiert und werden vor jedem Lauf versioniert gespeichert.">
    <button v-if="workspaceStore.canManage" class="button button-secondary" type="button" :disabled="saving || previewing" @click="preview">{{ previewing ? 'Vorschau startet …' : 'Vorschau / Dry-run' }}</button>
    <button v-if="workspaceStore.canManage" class="button button-primary" type="button" :disabled="saving" @click="save(false)">{{ saving ? 'Speichert …' : 'Speichern' }}</button>
  </PageHeader>
  <LoadingState v-if="loading" />
  <ErrorBanner v-else-if="error" :message="error" />
  <form v-if="!loading" class="editor-layout" @submit.prevent="save(false)">
    <nav class="editor-nav" aria-label="Profilbereiche"><a href="#general">Allgemein</a><a href="#matching">Event-Matching</a><a href="#placement">Platzierung</a><a href="#schedule">Zeitplan</a><a href="#notify">Benachrichtigungen</a></nav>
    <div class="editor-sections">
      <section id="general" class="card editor-section"><header><span>1</span><div><h2>Allgemein & Verbindungen</h2><p>WorshipTools ist die Quelle, ChurchTools das Ziel.</p></div></header><div class="form-grid">
        <label class="span-2"><span>Profilname</span><input v-model="form.name" required maxlength="120" /></label>
        <label><span>WorshipTools-Quelle</span><select v-model="form.source_connection_id" required><option value="" disabled>Auswählen …</option><option v-for="connection in sourceConnections" :key="connection.id" :value="connection.id">{{ connection.name }}</option></select><small v-if="!sourceConnections.length">Lege zuerst eine WorshipTools-Verbindung an.</small></label>
        <label><span>ChurchTools-Ziel</span><select v-model="form.target_connection_id" required @change="loadMetadata"><option value="" disabled>Auswählen …</option><option v-for="connection in targetConnections" :key="connection.id" :value="connection.id">{{ connection.name }}</option></select><small v-if="!targetConnections.length">Lege zuerst eine ChurchTools-Verbindung an.</small></label>
        <label class="check-card span-2"><input v-model="form.enabled" type="checkbox" /><span><strong>Profil aktiv</strong><small>Der Scheduler führt dieses Profil automatisch aus.</small></span></label>
      </div></section>

      <section id="matching" class="card editor-section"><header><span>2</span><div><h2>Event-Matching</h2><p>Mehrdeutige Treffer werden sicher übersprungen und als partiell gemeldet.</p></div></header><div class="form-grid">
        <label><span>Zuordnung</span><select v-model="form.match_mode"><option value="exact_time">Exakte Startzeit</option><option value="date_only">Nur lokales Datum</option></select></label>
        <label><span>Vorausschau</span><div class="input-suffix"><input v-model.number="form.lookahead_days" type="number" min="1" max="365" required /><span>Tage</span></div></label>
        <label><span>WorshipTools-Zeitzone</span><input v-model="form.source_timezone" required placeholder="Europe/Berlin" /></label>
        <label><span>ChurchTools-Zeitzone</span><input v-model="form.target_timezone" required placeholder="Europe/Berlin" /></label>
      </div>
      <div class="repeat-list"><article v-for="(rule, index) in form.event_rules" :key="index" class="repeat-card"><header><h3>Regel {{ index + 1 }}</h3><button v-if="form.event_rules.length > 1" class="link-button danger-text" type="button" @click="removeRule(index)">Entfernen</button></header><div class="form-grid">
        <label><span>Name enthält</span><input v-model="rule.name_contains" placeholder="z. B. Gottesdienst" /></label><label><span>Sichere Regex (optional)</span><input v-model="rule.name_regex" maxlength="256" placeholder="^Sonntag" /></label>
        <label><span>Kalender-IDs</span><select v-if="calendars.length" v-model="rule.calendar_ids" multiple><option v-for="option in calendars" :key="option.id" :value="option.id">{{ option.name }}</option></select><input v-else :value="rule.calendar_ids.join(', ')" placeholder="Kommagetrennte IDs" @input="rule.calendar_ids = ($event.target as HTMLInputElement).value.split(',').map(v => v.trim()).filter(Boolean)" /></label>
        <label><span>Campus-IDs</span><select v-if="campuses.length" v-model="rule.campus_ids" multiple><option v-for="option in campuses" :key="option.id" :value="option.id">{{ option.name }}</option></select><input v-else :value="rule.campus_ids.join(', ')" placeholder="Kommagetrennte IDs" @input="rule.campus_ids = ($event.target as HTMLInputElement).value.split(',').map(v => v.trim()).filter(Boolean)" /></label>
      </div></article><button class="button button-secondary button-small" type="button" @click="addRule">Weitere Regel</button></div></section>

      <section id="placement" class="card editor-section"><header><span>3</span><div><h2>Platzierung in ChurchTools</h2><p>Zugeordnete Song-Items werden verwaltet; fremde Header und Texte bleiben erhalten.</p></div></header>
        <div class="repeat-list"><article v-for="(placement, index) in form.placements" :key="index" class="repeat-card"><header><h3>Zielbereich {{ index + 1 }}</h3><button v-if="form.placements.length > 1" class="link-button danger-text" type="button" @click="removePlacement(index)">Entfernen</button></header><div class="form-grid">
          <label><span>Eindeutige Placement-ID</span><input v-model="placement.id" required maxlength="100" pattern="[A-Za-z0-9][A-Za-z0-9_.:-]*" placeholder="main" /></label>
          <label><span>Relation zum Anker</span><select v-model="placement.relation"><option value="before">Davor</option><option value="at">An dessen Position</option><option value="after">Danach</option></select></label>
          <label><span>Anker-Typ</span><input v-model="placement.anchor.item_type" placeholder="header" /></label><label><span>Anker-Titel</span><input v-model="placement.anchor.title" placeholder="Lobpreis" /></label>
          <label><span>Anker-Item-ID (optional)</span><input v-model="placement.anchor.item_id" placeholder="Remote-ID" /><small>Mindestens Typ, Titel oder Item-ID muss gesetzt sein.</small></label>
          <label><span>Erster Song (0-basiert)</span><input v-model.number="placement.song_start" type="number" min="0" required /></label>
          <label><span>Ende exklusiv (optional)</span><input v-model.number="placement.song_end" type="number" :min="placement.song_start" placeholder="alle übrigen" /></label>
        </div></article><button class="button button-secondary button-small" type="button" @click="addPlacement">Weiteren Zielbereich</button></div>
        <div class="form-grid standalone"><label class="check-card span-2"><input v-model="form.create_missing_songs" type="checkbox" /><span><strong>Fehlende Songs automatisch erstellen</strong><small>Nur eindeutige Treffer werden geschrieben; jeder Write wird anschließend verifiziert.</small></span></label>
          <label v-if="form.create_missing_songs"><span>ChurchTools-Songkategorie</span><select v-if="songCategories.length" v-model.number="form.song_category_id" required><option :value="null" disabled>Auswählen …</option><option v-for="option in songCategories" :key="option.id" :value="Number(option.id)">{{ option.name }}</option></select><input v-else v-model.number="form.song_category_id" type="number" min="1" required placeholder="Kategorie-ID" /><small>Pflichtfeld für automatisch angelegte Songs.</small></label>
          <label v-if="form.create_missing_songs"><span>Name des Standard-Arrangements</span><input v-model="form.arrangement_name" required maxlength="50" /></label>
        </div>
        <div class="form-grid standalone">
          <div class="span-2"><h3>Standardwerte für Agenda-Songs</h3><p>Diese optionalen Werte werden auf jedes vom Sync verwaltete Song-Item angewendet. Leere Felder verwenden den ChurchTools-Standard.</p></div>
          <label><span>Agenda-Titel</span><input v-model="form.agenda_item_defaults.title" maxlength="100" placeholder="Optionaler eigener Titel" /><small>Maximal 100 Zeichen.</small></label>
          <label><span>Verantwortlich</span><input v-model="form.agenda_item_defaults.responsible" maxlength="1000" placeholder="z. B. [Worship Leader]" /><small>Text oder ChurchTools-Platzhalter, maximal 1.000 Zeichen.</small></label>
          <label><span>Dauer</span><div class="input-suffix"><input v-model.number="form.agenda_item_defaults.duration" type="number" min="0" max="86400" step="1" placeholder="0" /><span>Sekunden</span></div><small>Zwischen 0 Sekunden und 24 Stunden.</small></label>
          <label class="span-2"><span>Agenda-Notiz</span><textarea v-model="form.agenda_item_defaults.note" rows="3" maxlength="4000" placeholder="Optionale Hinweise für das Team"></textarea><small>Maximal 4.000 Zeichen.</small></label>
        </div>
      </section>

      <section id="schedule" class="card editor-section"><header><span>4</span><div><h2>Zeitplan</h2><p>Das Mindestintervall beträgt 30 Minuten.</p></div></header><fieldset class="segmented"><legend>Art des Zeitplans</legend><button type="button" :class="{ active: form.schedule_type === 'interval' }" @click="form.schedule_type = 'interval'">Intervall</button><button type="button" :class="{ active: form.schedule_type === 'cron' }" @click="form.schedule_type = 'cron'">Cron-Ausdruck</button></fieldset><div class="form-grid schedule-fields">
        <label v-if="form.schedule_type === 'interval'"><span>Alle</span><div class="input-suffix"><input v-model.number="form.interval_minutes" type="number" min="30" max="10080" required /><span>Minuten</span></div></label>
        <label v-else><span>Cron-Ausdruck</span><input v-model="form.cron_expression" required placeholder="0 * * * *" /><small>Auswertung in der Zielzeitzone.</small></label>
      </div></section>

      <section id="notify" class="card editor-section"><header><span>5</span><div><h2>Benachrichtigungen</h2><p>In-App ist die kanonische Quelle. Telegram ist veraltet und standardmäßig aus.</p></div></header><div class="check-grid">
        <label class="check-card"><input :checked="true" type="checkbox" disabled /><span><strong>In-App</strong><small>Immer aktiv</small></span></label><label class="check-card"><input v-model="form.notification_preferences.email" type="checkbox" /><span><strong>E-Mail</strong><small>An deine Konto-Adresse</small></span></label><label class="check-card"><input v-model="form.notification_preferences.web_push" type="checkbox" /><span><strong>Web Push</strong><small>Nach Browserfreigabe</small></span></label><label class="check-card deprecated"><input v-model="form.notification_preferences.telegram" type="checkbox" /><span><strong>Telegram</strong><small>Veraltet / optional</small></span></label>
        <label class="check-card"><input v-model="form.notification_preferences.notify_new_songs" type="checkbox" /><span><strong>Neue Songs</strong><small>Informative Meldung</small></span></label><label class="check-card"><input v-model="form.notification_preferences.notify_success" type="checkbox" /><span><strong>Erfolgreiche Läufe</strong><small>Standardmäßig aus</small></span></label>
      </div></section>

      <div class="sticky-editor-actions"><span v-if="dirty">Ungespeicherte Änderungen</span><RouterLink class="button button-secondary" to="/profiles">Abbrechen</RouterLink><button v-if="workspaceStore.canManage" class="button button-primary" type="submit" :disabled="saving">{{ saving ? 'Speichert …' : 'Profil speichern' }}</button></div>
    </div>
  </form>
</template>
