<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import LoadingState from '@/components/LoadingState.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import EmptyState from '@/components/EmptyState.vue'
import { api, errorMessage } from '@/api/client'
import type { AppNotification, NotificationMarkAllReadResponse, NotificationPage, PushSubscriptionDevice, UserNotificationPreferences } from '@/api/types'
import { formatDateTime } from '@/utils/format'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useToastStore } from '@/stores/toasts'

const workspaceStore = useWorkspaceStore()
const toasts = useToastStore()
const notifications = ref<AppNotification[]>([])
const unread = ref(0)
const unreadOnly = ref(false)
const loading = ref(true)
const loadingMore = ref(false)
const markingAllRead = ref(false)
const error = ref<string | null>(null)
const savingPreferences = ref(false)
const preferences = ref<UserNotificationPreferences>({ in_app_enabled: true, email_enabled: true, push_enabled: false, telegram_enabled: false, success_notifications: false })
const pushDevices = ref<PushSubscriptionDevice[]>([])
const permission = ref(typeof Notification === 'undefined' ? 'unsupported' : Notification.permission)
const notificationTotal = ref(0)
const hasMore = computed(() => notifications.value.length < notificationTotal.value)

async function loadNotifications(append = false): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  const query = new URLSearchParams({
    limit: '100',
    offset: append ? String(notifications.value.length) : '0',
    unread_only: String(unreadOnly.value),
  })
  const page = await api.get<NotificationPage>(`/workspaces/${workspaceId}/notifications?${query}`, { workspaceId })
  notifications.value = append ? [...notifications.value, ...page.items] : page.items
  notificationTotal.value = page.total
  unread.value = page.unread
}

async function load(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  loading.value = true
  error.value = null
  try {
    const [notificationPage, preferenceData, devices] = await Promise.all([
      api.get<NotificationPage>(`/workspaces/${workspaceId}/notifications?limit=100&offset=0&unread_only=${unreadOnly.value}`, { workspaceId }),
      api.get<UserNotificationPreferences>(`/workspaces/${workspaceId}/notifications/preferences`, { workspaceId }),
      api.get<PushSubscriptionDevice[]>(`/workspaces/${workspaceId}/notifications/push-subscriptions`, { workspaceId }),
    ])
    notifications.value = notificationPage.items
    notificationTotal.value = notificationPage.total
    unread.value = notificationPage.unread
    preferences.value = { ...preferenceData, in_app_enabled: true }
    pushDevices.value = devices
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function markRead(notification: AppNotification): Promise<void> {
  if (notification.read_at) return
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  try {
    const updated = await api.post<AppNotification>(`/workspaces/${workspaceId}/notifications/${notification.id}/read`)
    Object.assign(notification, updated)
    unread.value = Math.max(0, unread.value - 1)
    if (unreadOnly.value) {
      notifications.value = notifications.value.filter((item) => item.id !== notification.id)
      notificationTotal.value = Math.max(0, notificationTotal.value - 1)
    }
  } catch (cause) {
    toasts.show('error', 'Benachrichtigung konnte nicht markiert werden', errorMessage(cause))
  }
}

async function markAllRead(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  markingAllRead.value = true
  try {
    const result = await api.post<NotificationMarkAllReadResponse>(`/workspaces/${workspaceId}/notifications/read-all`, undefined, { workspaceId })
    unread.value = 0
    if (unreadOnly.value) {
      notifications.value = []
      notificationTotal.value = 0
    } else {
      notifications.value = notifications.value.map((item) => item.read_at ? item : { ...item, read_at: result.read_at })
    }
    toasts.show('success', result.updated === 1 ? 'Eine Benachrichtigung als gelesen markiert' : `${result.updated} Benachrichtigungen als gelesen markiert`)
  } catch (cause) {
    toasts.show('error', 'Benachrichtigungen konnten nicht markiert werden', errorMessage(cause))
  } finally {
    markingAllRead.value = false
  }
}

async function loadMore(): Promise<void> {
  loadingMore.value = true
  try {
    await loadNotifications(true)
  } catch (cause) {
    toasts.show('error', 'Weitere Benachrichtigungen konnten nicht geladen werden', errorMessage(cause))
  } finally {
    loadingMore.value = false
  }
}

async function savePreferences(): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  savingPreferences.value = true
  try {
    preferences.value = await api.put<UserNotificationPreferences>(`/workspaces/${workspaceId}/notifications/preferences`, {
      ...preferences.value,
      in_app_enabled: true,
    })
    toasts.show('success', 'Benachrichtigungspräferenzen gespeichert')
  } catch (cause) {
    toasts.show('error', 'Präferenzen konnten nicht gespeichert werden', errorMessage(cause))
  } finally {
    savingPreferences.value = false
  }
}

function base64UrlToUint8Array(value: string): Uint8Array<ArrayBuffer> {
  const normalized = `${value}${'='.repeat((4 - value.length % 4) % 4)}`.replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(normalized)
  return Uint8Array.from([...raw].map((character) => character.charCodeAt(0)))
}

async function enablePush(): Promise<void> {
  if (!('Notification' in window) || !('serviceWorker' in navigator)) {
    toasts.show('warning', 'Web Push wird von diesem Browser nicht unterstützt')
    return
  }
  try {
    permission.value = await Notification.requestPermission()
    if (permission.value !== 'granted') return
    const workspaceId = workspaceStore.activeId
    if (!workspaceId) return
    const key = await api.get<{ public_key: string }>('/push/vapid-key')
    const registration = await navigator.serviceWorker.ready
    const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64UrlToUint8Array(key.public_key) })
    const serialized = subscription.toJSON()
    if (!serialized.endpoint || !serialized.keys?.p256dh || !serialized.keys.auth) throw new Error('Der Browser hat keine vollständige Push-Subscription geliefert.')
    await api.post(`/workspaces/${workspaceId}/notifications/push-subscriptions`, {
      endpoint: serialized.endpoint,
      p256dh: serialized.keys.p256dh,
      auth: serialized.keys.auth,
      device_name: navigator.userAgent.slice(0, 120) || 'Browser',
    })
    preferences.value.push_enabled = true
    await savePreferences()
    await load()
  } catch (cause) {
    toasts.show('error', 'Web Push konnte nicht aktiviert werden', errorMessage(cause))
  }
}

async function removePushDevice(device: PushSubscriptionDevice): Promise<void> {
  const workspaceId = workspaceStore.activeId
  if (!workspaceId) return
  try {
    await api.delete(`/workspaces/${workspaceId}/notifications/push-subscriptions/${device.id}`)
    pushDevices.value = pushDevices.value.filter((item) => item.id !== device.id)
    toasts.show('success', 'Push-Gerät entfernt')
  } catch (cause) {
    toasts.show('error', 'Push-Gerät konnte nicht entfernt werden', errorMessage(cause))
  }
}

watch(unreadOnly, async () => {
  try {
    await loadNotifications()
  } catch (cause) {
    toasts.show('error', 'Benachrichtigungen konnten nicht geladen werden', errorMessage(cause))
  }
})

onMounted(load)
</script>

<template>
  <PageHeader title="Benachrichtigungen" eyebrow="Notification Center" :description="`${unread} ungelesene Hinweise in diesem Workspace.`">
    <button v-if="unread" class="button button-secondary" type="button" :disabled="markingAllRead" @click="markAllRead">{{ markingAllRead ? 'Wird markiert …' : 'Alle als gelesen' }}</button>
  </PageHeader>
  <LoadingState v-if="loading" />
  <ErrorBanner v-else-if="error" :message="error" />
  <div v-else class="notification-layout">
    <section class="card notification-center">
      <div class="section-heading"><div><h2>Posteingang</h2><p>Fehler, Warnungen und wichtige Sync-Ereignisse</p></div><label class="check-label"><input v-model="unreadOnly" type="checkbox" /> <span>Nur ungelesene</span></label></div>
      <EmptyState v-if="!notifications.length" title="Keine Benachrichtigungen" text="Hier ist gerade alles erledigt." symbol="✓" />
      <ol v-else class="notification-list"><li v-for="notification in notifications" :key="notification.id" :class="[notification.severity, { unread: !notification.read_at }]"><span class="notification-symbol" aria-hidden="true">{{ notification.severity === 'error' ? '!' : notification.severity === 'warning' ? '△' : notification.severity === 'success' ? '✓' : 'i' }}</span><div><header><strong>{{ notification.title }}</strong><span>{{ formatDateTime(notification.created_at) }}</span></header><p>{{ notification.body }}</p><div><span class="category">{{ notification.category }}</span><span class="notification-actions"><button v-if="!notification.read_at" class="link-button" type="button" :aria-label="`Als gelesen markieren: ${notification.title}`" @click="markRead(notification)">Als gelesen</button><RouterLink v-if="notification.run_id" :to="`/runs/${notification.run_id}`">Zum Lauf →</RouterLink></span></div></div></li></ol>
      <button v-if="hasMore" class="button button-secondary button-wide" type="button" :disabled="loadingMore" @click="loadMore">{{ loadingMore ? 'Wird geladen …' : 'Weitere Benachrichtigungen laden' }}</button>
    </section>
    <aside class="card preferences-panel"><div class="section-heading"><div><h2>Präferenzen</h2><p>Gilt für alle Profile</p></div></div>
      <div class="preference-list"><label><span><strong>In-App</strong><small>Kanonische Quelle, immer aktiv</small></span><input :checked="true" type="checkbox" disabled /></label><label><span><strong>E-Mail</strong><small>Fehler und wichtige Warnungen</small></span><input v-model="preferences.email_enabled" type="checkbox" /></label><label><span><strong>Web Push</strong><small>{{ permission === 'granted' ? 'In diesem Browser erlaubt' : 'Browserfreigabe erforderlich' }}</small></span><input v-model="preferences.push_enabled" type="checkbox" :disabled="permission !== 'granted'" /></label><label class="deprecated"><span><strong>Telegram</strong><small>Veraltet / optional</small></span><input v-model="preferences.telegram_enabled" type="checkbox" /></label><hr /><label><span><strong>Erfolgreiche Läufe</strong><small>Kann bei vielen Profilen häufig sein</small></span><input v-model="preferences.success_notifications" type="checkbox" /></label></div>
      <div v-if="pushDevices.length" class="push-devices"><h3>Push-Geräte</h3><div v-for="device in pushDevices" :key="device.id"><span><strong>{{ device.device_name }}</strong><small>Hinzugefügt {{ formatDateTime(device.created_at) }}</small></span><button class="link-button danger-text" type="button" @click="removePushDevice(device)">Entfernen</button></div></div>
      <button v-if="permission !== 'granted' || !pushDevices.length" class="button button-secondary button-wide" type="button" @click="enablePush">Web Push auf diesem Gerät aktivieren</button><button class="button button-primary button-wide" type="button" :disabled="savingPreferences" @click="savePreferences">{{ savingPreferences ? 'Speichert …' : 'Präferenzen speichern' }}</button>
    </aside>
  </div>
</template>
