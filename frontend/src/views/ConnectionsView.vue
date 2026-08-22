<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import EmptyState from '@/components/EmptyState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useFocusTrap } from '@/composables/useFocusTrap'
import { api, ApiError, errorMessage, pagePath } from '@/api/client'
import type { Connection, ConnectionInput, Provider } from '@/api/types'
import { connectionForEdit, connectionPayload, connectionUpdatePayload, newConnection, resetCredentialFields } from '@/domain/connection'
import { formatDateTime } from '@/utils/format'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useToastStore } from '@/stores/toasts'

const workspaceStore = useWorkspaceStore()
const toasts = useToastStore()
const connections = ref<Connection[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const dialogOpen = ref(false)
const saving = ref(false)
const testingId = ref<string | null>(null)
const editing = ref<Connection | null>(null)
const secretChanged = ref(false)
const form = ref<ConnectionInput>(newConnection('churchtools'))
const offset = ref(0)
const total = ref(0)
const connectionDialog = ref<HTMLElement | null>(null)
const limit = 50
const pageNumber = computed(() => Math.floor(offset.value / limit) + 1)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / limit)))
const statusOf = (connection: Connection): 'healthy' | 'error' | 'unknown' => connection.last_test_succeeded === true ? 'healthy' : connection.last_test_succeeded === false ? 'error' : 'unknown'
const dialogTitle = computed(() => editing.value ? `${editing.value.name} bearbeiten` : 'Verbindung hinzufügen')

useFocusTrap(connectionDialog, () => dialogOpen.value, {
  onEscape: () => { dialogOpen.value = false },
  initialFocus: (container) => container.querySelector<HTMLElement>('input:not([disabled])'),
})

function deleteBlockedReason(connection: Connection): string | null {
  const blockers = connection.delete_blockers ?? []
  if (blockers.includes('profile_reference')) return 'Diese Verbindung wird von mindestens einem Sync-Profil verwendet.'
  if (blockers.includes('remote_binding')) return 'Diese Verbindung besitzt noch verwaltete Remote-Zuordnungen.'
  return null
}

async function load(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  loading.value = true
  error.value = null
  try {
    const page = await api.page<Connection>(pagePath(`/workspaces/${workspaceId}/connections`, limit, offset.value), { workspaceId })
    connections.value = page.items
    total.value = page.total
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

function openNew(provider: Provider): void {
  editing.value = null
  form.value = newConnection(provider)
  secretChanged.value = true
  dialogOpen.value = true
}

function openEdit(connection: Connection): void {
  editing.value = connection
  form.value = connectionForEdit(connection)
  secretChanged.value = false
  dialogOpen.value = true
}

function changeProvider(provider: Provider): void {
  if (editing.value) return
  form.value = newConnection(provider)
  secretChanged.value = true
}

function toggleSecretChange(): void {
  secretChanged.value = !secretChanged.value
  form.value = resetCredentialFields(form.value)
}

async function save(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  const payload = editing.value
    ? connectionUpdatePayload(form.value, secretChanged.value)
    : connectionPayload(form.value, secretChanged.value)
  if (secretChanged.value && !payload.credentials) {
    error.value = 'Gib mindestens einen neuen Zugangsdaten-Wert ein oder wähle „Nicht ändern“.'
    return
  }
  saving.value = true
  error.value = null
  try {
    if (editing.value) {
      await api.patch<Connection>(`/workspaces/${workspaceId}/connections/${editing.value.id}`, payload, {
        ifMatch: `"${editing.value.revision}"`,
        workspaceId,
      })
      toasts.show('success', 'Verbindung aktualisiert')
    } else {
      await api.post<Connection>(`/workspaces/${workspaceId}/connections`, payload)
      toasts.show('success', 'Verbindung angelegt', 'Teste sie jetzt, bevor du ein Profil aktivierst.')
    }
    dialogOpen.value = false
    await load()
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 412) {
      await load()
      toasts.show('warning', 'Verbindung wurde zwischenzeitlich geändert', 'Die aktuelle Version wurde neu geladen.')
    }
    error.value = errorMessage(cause)
  } finally {
    saving.value = false
  }
}

async function testConnection(connection: Connection): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  testingId.value = connection.id
  try {
    const result = await api.post<{ succeeded: boolean; message: string }>(`/workspaces/${workspaceId}/connections/${connection.id}/test`)
    toasts.show(result.succeeded ? 'success' : 'error', result.succeeded ? 'Verbindung erfolgreich' : 'Verbindung fehlgeschlagen', result.message)
    await load()
  } catch (cause) {
    toasts.show('error', 'Verbindungstest fehlgeschlagen', errorMessage(cause))
  } finally {
    testingId.value = null
  }
}

async function removeConnection(connection: Connection): Promise<void> {
  const blockedReason = deleteBlockedReason(connection)
  if (blockedReason) {
    toasts.show('warning', 'Verbindung wird noch verwendet', blockedReason)
    return
  }
  if (!confirm(`Unbenutzte Verbindung „${connection.name}“ dauerhaft löschen?`)) return
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  try {
    await api.delete(`/workspaces/${workspaceId}/connections/${connection.id}`, {
      ifMatch: `"${connection.revision}"`,
      workspaceId,
    })
    toasts.show('success', 'Verbindung gelöscht')
    if (connections.value.length === 1 && offset.value > 0) offset.value = Math.max(0, offset.value - limit)
    await load()
  } catch (cause) {
    toasts.show('error', 'Verbindung konnte nicht gelöscht werden', errorMessage(cause))
  }
}

function changePage(direction: -1 | 1): void {
  offset.value = Math.max(0, offset.value + direction * limit)
  void load()
}

onMounted(load)
</script>

<template>
  <PageHeader title="Verbindungen" eyebrow="Integrationen" description="Zugangsdaten sind write-only: Nach dem Speichern werden sie nie wieder an den Browser ausgeliefert.">
    <button v-if="workspaceStore.canManage" class="button button-primary" type="button" @click="openNew('churchtools')">Verbindung hinzufügen</button>
  </PageHeader>
  <ErrorBanner v-if="error" :message="error" />
  <LoadingState v-if="loading" />
  <EmptyState v-else-if="!connections.length && total === 0" title="Noch keine Verbindung" text="Lege je eine Verbindung zu WorshipTools und ChurchTools an." symbol="⌁"><button v-if="workspaceStore.canManage" class="button button-primary" type="button" @click="openNew('churchtools')">Erste Verbindung</button></EmptyState>
  <section v-else class="connection-grid">
    <article v-for="connection in connections" :key="connection.id" class="card connection-card">
      <header><span class="provider-logo" :class="connection.provider === 'churchtools' ? 'ct' : 'wt'">{{ connection.provider === 'churchtools' ? 'CT' : 'WT' }}</span><div><h2>{{ connection.name }}</h2><span>{{ connection.provider === 'churchtools' ? 'ChurchTools' : 'WorshipTools' }}</span></div><StatusBadge :status="statusOf(connection)" /></header>
      <dl><div><dt>Adresse</dt><dd>{{ connection.provider === 'churchtools' ? (connection.base_url || 'Fehlt') : 'Feste WorshipTools-Endpunkte' }}</dd></div><div><dt>Zugangsdaten</dt><dd>{{ connection.credential_hint || (connection.credentials_configured ? 'Hinterlegt' : 'Fehlen') }}</dd></div><div><dt>Letzter Test</dt><dd>{{ formatDateTime(connection.last_tested_at) }}</dd></div></dl>
      <p v-if="connection.last_test_message" class="connection-message">{{ connection.last_test_message }}</p>
      <footer><button v-if="workspaceStore.canManage" class="button button-secondary button-small" type="button" :disabled="testingId === connection.id" @click="testConnection(connection)">{{ testingId === connection.id ? 'Test läuft …' : 'Verbindung testen' }}</button><div v-if="workspaceStore.canManage"><button class="link-button" type="button" @click="openEdit(connection)">Bearbeiten</button><button class="link-button danger-text" type="button" :disabled="Boolean(deleteBlockedReason(connection))" :title="deleteBlockedReason(connection) ?? 'Verbindung löschen'" @click="removeConnection(connection)">Löschen</button></div></footer>
      <small v-if="deleteBlockedReason(connection)" class="table-sub">{{ deleteBlockedReason(connection) }}</small>
    </article>
  </section>
  <nav v-if="total > limit" class="pagination" aria-label="Verbindungsseiten"><button class="button button-small button-secondary" type="button" :disabled="offset === 0" @click="changePage(-1)">Zurück</button><span>Seite {{ pageNumber }} von {{ totalPages }}</span><button class="button button-small button-secondary" type="button" :disabled="offset + limit >= total" @click="changePage(1)">Weiter</button></nav>

  <div v-if="dialogOpen" class="dialog-backdrop" @click.self="dialogOpen = false">
    <section ref="connectionDialog" class="dialog" role="dialog" aria-modal="true" aria-labelledby="connection-dialog-title" tabindex="-1">
      <header><div><p class="eyebrow">Integration</p><h2 id="connection-dialog-title">{{ dialogTitle }}</h2></div><button class="icon-button" type="button" aria-label="Dialog schließen" @click="dialogOpen = false">×</button></header>
      <form class="stack-form" @submit.prevent="save">
        <fieldset v-if="!editing" class="segmented"><legend>Provider</legend><button type="button" :class="{ active: form.provider === 'churchtools' }" @click="changeProvider('churchtools')">ChurchTools</button><button type="button" :class="{ active: form.provider === 'worshiptools' }" @click="changeProvider('worshiptools')">WorshipTools</button></fieldset>
        <label><span>Anzeigename</span><input v-model="form.name" required maxlength="120" /></label>
        <label v-if="form.provider === 'churchtools'"><span>ChurchTools-HTTPS-Adresse</span><input v-model="form.base_url" type="url" placeholder="https://gemeinde.church.tools" required /></label>
        <div v-else class="secret-notice"><span>⌁</span><p><strong>Feste WorshipTools-Endpunkte</strong><br />Die Dienstadressen werden sicher vom Adapter verwaltet.</p></div>
        <div v-if="editing" class="secret-notice"><span>🔒</span><p><strong>Zugangsdaten sind hinterlegt.</strong><br />{{ editing.credential_hint || 'Sie werden nicht angezeigt.' }}</p><button class="link-button" type="button" @click="toggleSecretChange">{{ secretChanged ? 'Nicht ändern' : 'Neue Zugangsdaten' }}</button></div>
        <template v-if="!editing || secretChanged">
          <label v-if="form.provider === 'churchtools'"><span>Login-Token</span><input v-model="form.credentials!.token" type="password" autocomplete="new-password" :required="!editing" /></label>
          <template v-else><label><span>E-Mail-Adresse</span><input v-model="form.credentials!.email" type="email" autocomplete="off" :required="!editing" /><small v-if="editing">Leer lassen, um sie beizubehalten.</small></label><label><span>Passwort</span><input v-model="form.credentials!.password" type="password" autocomplete="new-password" :required="!editing" /><small v-if="editing">Nur tatsächlich eingegebene Werte werden gesendet.</small></label><label><span>WorshipTools Account-ID</span><input v-model="form.credentials!.account_id" autocomplete="off" :required="!editing" placeholder="Account-ID der Organisation" /><small v-if="editing">Leer lassen, um sie beizubehalten.</small></label></template>
        </template>
        <div class="dialog-actions"><button class="button button-secondary" type="button" @click="dialogOpen = false">Abbrechen</button><button class="button button-primary" type="submit" :disabled="saving">{{ saving ? 'Speichert …' : 'Speichern' }}</button></div>
      </form>
    </section>
  </div>
</template>
