import { expect, test, type Page, type Route } from '@playwright/test'

const user = { id: 'user-1', email: 'admin@gemeinde.de', email_verified_at: '2026-01-01T10:00:00Z', is_platform_admin: false, totp_enabled: true }
const workspace = { id: 'workspace-1', name: 'Gemeinde Musterstadt', slug: 'gemeinde-musterstadt', archived_at: null, profile_quota: 3, member_quota: 10, role: 'owner', created_at: '2026-01-01T10:00:00Z', updated_at: '2026-01-01T10:00:00Z' }
const profile = {
  id: 'profile-1',
  workspace_id: workspace.id,
  name: 'Sonntags-Sync',
  enabled: true,
  source_connection_id: 'source-1',
  target_connection_id: 'target-1',
  sync_mode: 'source_changes_only',
  match_mode: 'exact_time',
  source_timezone: 'Europe/Berlin',
  target_timezone: 'Europe/Berlin',
  lookahead_days: 28,
  schedule_type: 'interval',
  interval_minutes: 60,
  cron_expression: null,
  next_scheduled_at: null,
  event_rules: [],
  placements: [],
  notification_preferences: { in_app: true, web_push: true, email: true, telegram: false, notify_success: false, notify_new_songs: true },
  create_missing_songs: true,
  song_category_id: 4,
  arrangement_name: 'Standard-Arrangement',
  agenda_item_defaults: { title: null, note: null, responsible: null, duration: null },
  delete_blockers: [],
  revision: 1,
  created_at: '2026-01-01T10:00:00Z',
  updated_at: '2026-01-01T10:00:00Z',
}

async function json(route: Route, body: unknown, status = 200, headers: Record<string, string> = {}): Promise<void> {
  await route.fulfill({ status, headers, contentType: 'application/json', body: JSON.stringify(body) })
}

async function mockAuthenticatedApi(page: Page): Promise<void> {
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/auth/me')) return json(route, user)
    if (path.endsWith('/workspaces')) return json(route, { items: [workspace], total: 1, limit: 50, offset: 0 })
    if (path.endsWith('/profiles')) return json(route, { items: [], total: 0, limit: 100, offset: 0 })
    if (path.endsWith('/runs')) return json(route, { items: [], total: 0, limit: 20, offset: 0 })
    if (path.endsWith('/connections')) return json(route, { items: [], total: 0, limit: 100, offset: 0 })
    if (path.endsWith('/notifications')) return json(route, { items: [], total: 0, unread: 0, limit: 5, offset: 0 })
    return json(route, { title: 'Nicht gefunden', status: 404 }, 404)
  })
}

test('öffentliche Anmeldung ist deutsch und zugänglich', async ({ page }) => {
  await page.route('**/api/v1/auth/me', (route) => json(route, { title: 'Nicht angemeldet', status: 401 }, 401))
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: 'Willkommen zurück' })).toBeVisible()
  await expect(page.getByLabel('E-Mail-Adresse')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Anmelden' })).toBeVisible()
  await expect(page.locator('html')).toHaveAttribute('lang', 'de')
})

test('Dashboard trennt und zeigt den aktiven Workspace', async ({ page }) => {
  await mockAuthenticatedApi(page)
  await page.goto('/dashboard')
  await expect(page.getByRole('heading', { name: /Gemeinde Musterstadt/ })).toBeVisible()
  await expect(page.getByText('Noch kein Profil eingerichtet')).toBeVisible()
  if ((page.viewportSize()?.width ?? 1024) <= 820) {
    await expect(page.getByLabel('Hauptnavigation')).toBeHidden()
    await expect(page.getByRole('button', { name: 'Navigation öffnen' })).toBeVisible()
  } else {
    await expect(page.getByLabel('Hauptnavigation')).toBeVisible()
  }
})

test('mobile Navigation bleibt bedienbar', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockAuthenticatedApi(page)
  await page.goto('/dashboard')
  const menuButton = page.getByRole('button', { name: 'Navigation öffnen' })
  await menuButton.click()
  await expect(page.getByLabel('Hauptnavigation')).toBeVisible()
  await expect(page.locator('#main-navigation')).toBeFocused()
  await expect(page.getByRole('link', { name: /Historie/ })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByLabel('Hauptnavigation')).toBeHidden()
  await expect(menuButton).toBeFocused()

  await menuButton.click()
  await page.getByRole('link', { name: /Historie/ }).click()
  await expect(page).toHaveURL('/runs')
  await expect(page).toHaveTitle('Sync-Historie · WorshipTool Sync')
  await expect(page.getByRole('heading', { name: 'Sync-Historie' })).toBeFocused()
  await expect(page.locator('#route-announcer')).toHaveText('Sync-Historie geladen')
})

test('Verbindungsdialog hält den Fokus und stellt ihn nach Escape wieder her', async ({ page }) => {
  await mockAuthenticatedApi(page)
  await page.goto('/connections')
  const trigger = page.getByRole('button', { name: 'Verbindung hinzufügen' })
  await trigger.click()

  await expect(page.getByRole('dialog', { name: 'Verbindung hinzufügen' })).toBeVisible()
  await expect(page.getByLabel('Anzeigename')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'Verbindung hinzufügen' })).toBeHidden()
  await expect(trigger).toBeFocused()
})

test('Plattform-Admin ändert Workspace-Nutzung über die geschützte API', async ({ page }) => {
  const admin = { ...user, is_platform_admin: true }
  const adminWorkspace = { ...workspace, profile_count: 2, member_count: 4, manual_run_cooldown_seconds: 1800 as const }
  let updateBody: unknown
  await page.route('**/health/ready', (route) => json(route, { status: 'ok', database: 'ok', redis: 'ok', version: '1.0.0' }))
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/auth/me')) return json(route, admin)
    if (path.endsWith('/workspaces')) return json(route, { items: [workspace], total: 1, limit: 50, offset: 0 })
    if (path.endsWith('/admin/workspaces')) return json(route, { items: [adminWorkspace], total: 1, limit: 50, offset: 0 })
    if (path.endsWith(`/admin/workspaces/${workspace.id}/quotas`) && route.request().method() === 'PATCH') {
      updateBody = route.request().postDataJSON()
      return json(route, { ...adminWorkspace, profile_quota: 7, member_quota: 20, manual_run_cooldown_seconds: 300 })
    }
    return json(route, { title: 'Nicht gefunden', status: 404 }, 404)
  })

  await page.goto('/system')
  await expect(page.getByRole('heading', { name: 'Workspace-Nutzung' })).toBeVisible()
  const quotaTrigger = page.getByRole('button', { name: 'Nutzung ändern' })
  await quotaTrigger.click()
  await expect(page.getByLabel('Maximale Sync-Profile')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toBeHidden()
  await expect(quotaTrigger).toBeFocused()

  await quotaTrigger.click()
  await page.getByLabel('Maximale Sync-Profile').fill('7')
  await page.getByLabel('Maximale Mitglieder').fill('20')
  await page.getByLabel('Cooldown für manuelle Runs').selectOption('300')
  await page.getByRole('button', { name: 'Nutzung speichern' }).click()

  expect(updateBody).toEqual({ profile_quota: 7, member_quota: 20, manual_run_cooldown_seconds: 300 })
  await expect(page.getByRole('cell', { name: '2 / 7' })).toBeVisible()
  await expect(page.getByRole('cell', { name: '4 / 20' })).toBeVisible()
  await expect(page.getByRole('cell', { name: '5 Minuten' })).toBeVisible()
})

test('einzelne Benachrichtigung ist per Tastatur als gelesen markierbar', async ({ page }) => {
  const notification = {
    id: 'notification-1', workspace_id: workspace.id, user_id: user.id, severity: 'warning',
    category: 'sync', title: 'Prüfung erforderlich', body: 'Ein Event war mehrdeutig.', data: {},
    read_at: null, created_at: '2026-01-01T10:00:00Z', run_id: null, profile_id: profile.id,
  }
  let markedRead = false
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/auth/me')) return json(route, user)
    if (path.endsWith('/workspaces')) return json(route, { items: [workspace], total: 1, limit: 50, offset: 0 })
    if (path.endsWith(`/notifications/${notification.id}/read`) && request.method() === 'POST') {
      markedRead = true
      return json(route, { ...notification, read_at: '2026-01-01T10:05:00Z' })
    }
    if (path.endsWith('/notifications/preferences')) {
      return json(route, { in_app_enabled: true, email_enabled: true, push_enabled: false, telegram_enabled: false, success_notifications: false })
    }
    if (path.endsWith('/notifications/push-subscriptions')) return json(route, [])
    if (path.endsWith('/notifications')) {
      return json(route, { items: [notification], total: 1, unread: 1, limit: 100, offset: 0 })
    }
    return json(route, { title: 'Nicht gefunden', status: 404 }, 404)
  })

  await page.goto('/notifications')
  const markButton = page.getByRole('button', { name: 'Als gelesen markieren: Prüfung erforderlich' })
  await markButton.focus()
  await markButton.press('Enter')
  await expect(markButton).toHaveCount(0)
  expect(markedRead).toBe(true)
})

test('persistierter Lauf wird trotz Queue-503 geöffnet', async ({ page }) => {
  const queuedRun = {
    id: 'run-queued', workspace_id: workspace.id, profile_id: profile.id, status: 'queued', trigger: 'manual', dry_run: false,
    created_at: '2026-01-01T10:00:00Z', plan: null, error: null, actions: [], config_revision: 1,
  }
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/auth/me')) return json(route, user)
    if (path.endsWith('/workspaces')) return json(route, { items: [workspace], total: 1, limit: 50, offset: 0 })
    if (path.endsWith('/profiles')) return json(route, { items: [profile], total: 1, limit: 200, offset: 0 })
    if (path.endsWith(`/profiles/${profile.id}/runs`) && request.method() === 'POST') {
      return json(
        route,
        { title: 'Queue nicht erreichbar', status: 503, code: 'queue_unavailable' },
        503,
        { Location: `/api/v1/workspaces/${workspace.id}/runs/${queuedRun.id}` },
      )
    }
    if (path.endsWith(`/runs/${queuedRun.id}`)) return json(route, queuedRun)
    if (path.endsWith('/runs')) return json(route, { items: [], total: 0, limit: 20, offset: 0 })
    if (path.endsWith('/connections')) return json(route, { items: [], total: 0, limit: 200, offset: 0 })
    if (path.endsWith('/notifications')) return json(route, { items: [], total: 0, unread: 0, limit: 5, offset: 0 })
    return json(route, { title: 'Nicht gefunden', status: 404 }, 404)
  })

  await page.goto('/dashboard')
  await page.getByRole('button', { name: 'Jetzt syncen' }).click()
  await expect(page).toHaveURL(`/runs/${queuedRun.id}`)
  await expect(page.getByText('Sync gespeichert, Queue verzögert')).toBeVisible()
})

test('Operator darf ausführen, aber keine Konfiguration öffnen', async ({ page }) => {
  const operatorWorkspace = { ...workspace, role: 'operator' }
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/auth/me')) return json(route, user)
    if (path.endsWith('/workspaces')) return json(route, { items: [operatorWorkspace], total: 1, limit: 50, offset: 0 })
    if (path.endsWith('/profiles')) return json(route, { items: [profile], total: 1, limit: 50, offset: 0 })
    if (path.endsWith('/runs')) return json(route, { items: [], total: 0, limit: 200, offset: 0 })
    return json(route, { title: 'Nicht gefunden', status: 404 }, 404)
  })

  await page.goto('/connections')
  await expect(page).toHaveURL('/profiles')
  await expect(page.getByRole('button', { name: 'Vorschau' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Jetzt syncen' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Bearbeiten' })).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Neues Profil' })).toHaveCount(0)
})

test('Onboarding setzt gespeicherte Verbindungen und ein deaktiviertes Profil fort', async ({ page }) => {
  const source = {
    id: 'source-1', workspace_id: workspace.id, provider: 'worshiptools', name: 'WorshipTools', settings: {},
    credentials_configured: true, credential_hint: 'sync@example.org', revision: 1, last_tested_at: null,
    last_test_succeeded: null, last_test_message: null, delete_blockers: ['profile_reference'],
    created_at: profile.created_at, updated_at: profile.updated_at,
  }
  const target = {
    ...source, id: 'target-1', provider: 'churchtools', name: 'ChurchTools', base_url: 'https://example.church.tools',
    credential_hint: 'Login-Token hinterlegt',
  }
  const disabledProfile = {
    ...profile,
    enabled: false,
    placements: [{
      id: 'main',
      anchor: { item_type: 'header', title: 'Lobpreis' },
      relation: 'after',
      song_start: 0,
      song_end: null,
    }],
  }
  const requests: string[] = []
  let profileRevision = disabledProfile.revision
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    requests.push(`${request.method()} ${path}`)
    if (path.endsWith('/auth/me')) return json(route, user)
    if (path.endsWith('/workspaces')) return json(route, { items: [workspace], total: 1, limit: 50, offset: 0 })
    if (path.endsWith('/connections') && request.method() === 'GET') return json(route, { items: [source, target], total: 2, limit: 200, offset: 0 })
    if (path.endsWith(`/connections/${source.id}`) && request.method() === 'PATCH') {
      expect(request.headers()['if-match']).toBeUndefined()
      return json(route, { ...source, revision: 2 })
    }
    if (path.endsWith(`/connections/${target.id}`) && request.method() === 'PATCH') {
      expect(request.headers()['if-match']).toBeUndefined()
      return json(route, { ...target, revision: 2 })
    }
    if (path.endsWith('/test')) return json(route, { succeeded: true, message: 'Verbunden' })
    if (path.endsWith(`/connections/${target.id}/metadata`)) return json(route, { data: { calendars: [], campuses: [], song_categories: [{ id: '4', name: 'Lobpreis' }] }, retrieved_at: profile.updated_at })
    if (path.endsWith('/profiles') && request.method() === 'GET') return json(route, { items: [disabledProfile], total: 1, limit: 200, offset: 0 })
    if (path.endsWith(`/profiles/${profile.id}`) && request.method() === 'PATCH') {
      expect(request.headers()['if-match']).toBeUndefined()
      profileRevision += 1
      return json(route, { ...disabledProfile, revision: profileRevision })
    }
    if (path.endsWith(`/profiles/${profile.id}`) && request.method() === 'GET') return json(route, { ...disabledProfile, revision: profileRevision })
    if (path.endsWith(`/profiles/${profile.id}/preview`)) {
      return json(route, { id: 'preview-1', workspace_id: workspace.id, profile_id: profile.id, status: 'succeeded', trigger: 'manual', dry_run: true, created_at: profile.created_at, plan: {}, error: null, actions: [], config_revision: 2 })
    }
    if (path.endsWith('/runs/preview-1/actions')) return json(route, { items: [], total: 0, limit: 8, offset: 0, status_counts: {} })
    return json(route, { title: 'Nicht gefunden', status: 404 }, 404)
  })

  await page.goto('/onboarding')
  await page.getByLabel('ChurchTools-Adresse').fill('https://example.church.tools')
  await page.getByLabel('Login-Token').fill('ct-secret')
  await page.getByLabel('E-Mail-Adresse').fill('sync@example.org')
  await page.getByLabel('Passwort').fill('wt-secret')
  await page.getByLabel('WorshipTools Account-ID').fill('tenant-one')
  await page.getByRole('button', { name: 'Verbinden und weiter' }).click()
  await expect(page.getByRole('heading', { name: 'Erstes Sync-Profil' })).toBeVisible()
  await expect(page.getByText('Alle Songs landen nach dem ChurchTools-Header „Lobpreis“.')).toBeVisible()
  await page.getByText('Wo finde ich die frühere YAML-Konfiguration?').click()
  await expect(page.getByText('song_placements')).toBeVisible()
  await expect(page.getByText('Platzierung in ChurchTools')).toBeVisible()
  await page.getByRole('button', { name: 'Speichern und Vorschau' }).click()
  await expect(page.getByText('Vorschau abgeschlossen')).toBeVisible()

  const previewsBeforeEditor = requests.filter((request) => request.endsWith(`/profiles/${profile.id}/preview`)).length
  await page.getByRole('button', { name: 'Zurück' }).click()
  await page.getByRole('button', { name: 'Alle Einstellungen bearbeiten' }).click()
  await expect(page).toHaveURL(`/profiles/${profile.id}`)
  expect(requests.filter((request) => request.endsWith(`/profiles/${profile.id}/preview`))).toHaveLength(previewsBeforeEditor)
  await expect(page.getByText('Innerhalb einer Regel müssen alle gesetzten Filter passen')).toBeVisible()

  const songStart = page.getByLabel('Erster Song (0-basiert)', { exact: false })
  const songEnd = page.getByLabel('Ende exklusiv (optional)', { exact: false })
  await songStart.fill('0')
  await songEnd.fill('-1')
  await expect(page.getByText('Auswahl: Alle Songs außer dem letzten werden verwendet.')).toBeVisible()
  await songStart.fill('-1')
  await songEnd.fill('')
  await expect(page.getByText('Auswahl: Nur der letzte Song wird verwendet.')).toBeVisible()

  expect(requests).not.toContain(`POST /api/v1/workspaces/${workspace.id}/connections`)
  expect(requests).not.toContain(`POST /api/v1/workspaces/${workspace.id}/profiles`)
})
