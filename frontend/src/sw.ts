// @ts-nocheck -- This module deliberately compiles against the Service Worker global, not Window.
/// <reference lib="webworker" />

declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<{ url: string; revision?: string | null }>
}

const PRECACHE_ENTRIES = self.__WB_MANIFEST
const PRECACHE_URLS = PRECACHE_ENTRIES.map((entry) => new URL(entry.url, self.location.origin).pathname)

function releaseCacheName(): string {
  // A waiting worker must never mutate the active worker's cache. Deriving the
  // name from the injected manifest keeps both releases isolated until the
  // user explicitly activates the update.
  const release = PRECACHE_ENTRIES
    .map((entry) => `${entry.url}:${entry.revision ?? ''}`)
    .join('|')
  let hash = 0x811c9dc5
  for (let index = 0; index < release.length; index += 1) {
    hash = Math.imul(hash ^ release.charCodeAt(index), 0x01000193)
  }
  return `worshiptool-sync-shell-${(hash >>> 0).toString(16)}`
}

const CACHE_NAME = releaseCacheName()

function notificationPath(value: unknown): string {
  if (typeof value !== 'string') return '/notifications'
  try {
    const target = new URL(value, self.location.origin)
    if (target.origin !== self.location.origin) return '/notifications'
    return `${target.pathname}${target.search}${target.hash}`
  } catch {
    return '/notifications'
  }
}

self.addEventListener('install', (event: ExtendableEvent) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)))
})

self.addEventListener('message', (event: ExtendableMessageEvent) => {
  if (event.data?.type === 'SKIP_WAITING') void self.skipWaiting()
})

self.addEventListener('activate', (event: ExtendableEvent) => {
  event.waitUntil((async () => {
    const cacheNames = await caches.keys()
    await Promise.all(cacheNames
      .filter((key) => key.startsWith('worshiptool-sync-shell-') && key !== CACHE_NAME)
      .map((key) => caches.delete(key)))
    const currentCache = await caches.open(CACHE_NAME)
    const cachedRequests = await currentCache.keys()
    await Promise.all(cachedRequests
      .filter((request) => !PRECACHE_URLS.includes(new URL(request.url).pathname))
      .map((request) => currentCache.delete(request)))
    await self.clients.claim()
  })())
})

self.addEventListener('fetch', (event: FetchEvent) => {
  const request = event.request
  if (request.method !== 'GET') return
  const url = new URL(request.url)

  // Configuration, history, user data and secrets always go straight to the API.
  if (url.origin === self.location.origin && url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(request))
    return
  }

  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(async () => {
      const cache = await caches.open(CACHE_NAME)
      return (await cache.match('/index.html')) ?? Response.error()
    }))
    return
  }

  if (url.origin === self.location.origin && PRECACHE_URLS.includes(url.pathname)) {
    event.respondWith(caches.open(CACHE_NAME)
      .then((cache) => cache.match(request))
      .then((cached) => cached ?? fetch(request)))
  }
})

self.addEventListener('push', (event: PushEvent) => {
  let payload: { title?: string; body?: string; data?: { url?: string }; tag?: string } = {}
  try {
    payload = event.data?.json() ?? {}
  } catch {
    payload = { body: event.data?.text() }
  }
  const targetUrl = notificationPath(payload.data?.url)
  event.waitUntil(self.registration.showNotification(payload.title || 'WorshipTool Sync', {
    body: payload.body || 'Es gibt eine neue Benachrichtigung.',
    icon: '/pwa-192.png',
    badge: '/favicon.svg',
    tag: payload.tag,
    data: { url: targetUrl },
  }))
})

self.addEventListener('notificationclick', (event: NotificationEvent) => {
  event.notification.close()
  const path = notificationPath(event.notification.data?.url)
  const target = new URL(path, self.location.origin).href
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const existing = windows.find((client) => new URL(client.url).origin === self.location.origin)
    if (existing) {
      await existing.navigate(target)
      return existing.focus()
    }
    return self.clients.openWindow(target)
  })())
})

export {}
