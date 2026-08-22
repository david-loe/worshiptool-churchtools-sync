import { nextTick } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import AppShell from '@/components/AppShell.vue'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspaces'
import { memberWorkspaceFromQuery } from '@/domain/workspace'

const router = createRouter({
  history: createWebHistory(),
  scrollBehavior: () => ({ top: 0 }),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true, title: 'Anmelden' } },
    { path: '/registrieren', name: 'register', component: () => import('@/views/RegisterView.vue'), meta: { public: true, title: 'Registrieren' } },
    { path: '/passwort-vergessen', name: 'recovery', component: () => import('@/views/RecoveryView.vue'), meta: { public: true, title: 'Passwort zurücksetzen' } },
    { path: '/passwort-zuruecksetzen', name: 'recovery-confirm', component: () => import('@/views/ResetPasswordView.vue'), meta: { public: true, title: 'Neues Passwort' } },
    { path: '/email-bestaetigen', name: 'verify-email', component: () => import('@/views/VerifyEmailView.vue'), meta: { public: true, title: 'E-Mail bestätigen' } },
    { path: '/invite', name: 'invite', component: () => import('@/views/InviteView.vue'), meta: { requiresAuth: true, title: 'Einladung' } },
    { path: '/onboarding', name: 'onboarding', component: () => import('@/views/OnboardingView.vue'), meta: { requiresAuth: true, title: 'Einrichtung' } },
    {
      path: '/',
      component: AppShell,
      meta: { requiresAuth: true },
      children: [
        { path: '', redirect: '/dashboard' },
        { path: 'dashboard', name: 'dashboard', component: () => import('@/views/DashboardView.vue'), meta: { requiresWorkspace: true, title: 'Übersicht' } },
        { path: 'connections', name: 'connections', component: () => import('@/views/ConnectionsView.vue'), meta: { requiresWorkspace: true, workspaceAdmin: true, title: 'Verbindungen' } },
        { path: 'profiles', name: 'profiles', component: () => import('@/views/ProfilesView.vue'), meta: { requiresWorkspace: true, title: 'Sync-Profile' } },
        { path: 'profiles/new', name: 'profile-new', component: () => import('@/views/ProfileEditorView.vue'), meta: { requiresWorkspace: true, workspaceAdmin: true, title: 'Neues Sync-Profil' } },
        { path: 'profiles/:id', name: 'profile-edit', component: () => import('@/views/ProfileEditorView.vue'), meta: { requiresWorkspace: true, workspaceAdmin: true, title: 'Sync-Profil bearbeiten' } },
        { path: 'runs', name: 'runs', component: () => import('@/views/RunsView.vue'), meta: { requiresWorkspace: true, title: 'Sync-Historie' } },
        { path: 'runs/:id', name: 'run-detail', component: () => import('@/views/RunDetailView.vue'), meta: { requiresWorkspace: true, title: 'Laufdetails' } },
        { path: 'notifications', name: 'notifications', component: () => import('@/views/NotificationsView.vue'), meta: { requiresWorkspace: true, title: 'Benachrichtigungen' } },
        { path: 'workspaces', name: 'workspaces', component: () => import('@/views/WorkspacesView.vue'), meta: { requiresWorkspace: true, title: 'Workspace & Team' } },
        { path: 'account', name: 'account', component: () => import('@/views/AccountView.vue'), meta: { title: 'Mein Konto' } },
        { path: 'system', name: 'system', component: () => import('@/views/SystemView.vue'), meta: { platformAdmin: true, title: 'Systemverwaltung' } },
      ],
    },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('@/views/NotFoundView.vue'), meta: { public: true, title: 'Seite nicht gefunden' } },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  try {
    await auth.ensureSession()
  } catch {
    if (to.meta.public) return true
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  if (to.meta.public && auth.isAuthenticated && (to.name === 'login' || to.name === 'register')) return { name: 'dashboard' }
  if (to.meta.requiresAuth && !auth.isAuthenticated) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.requiresAuth) {
    const workspaces = useWorkspaceStore()
    if (!workspaces.workspaces.length) await workspaces.load()
    const requestedWorkspace = memberWorkspaceFromQuery(
      to.query.workspace,
      workspaces.workspaces.map((item) => item.id),
    )
    if (requestedWorkspace) {
      workspaces.select(requestedWorkspace)
    }
    if (to.meta.requiresWorkspace && !workspaces.activeId) return { name: 'onboarding' }
    if (to.meta.workspaceAdmin && !workspaces.canManage) return { name: 'profiles' }
    if (to.name === 'onboarding' && workspaces.activeId && !workspaces.canManage) return { name: 'dashboard' }
    if (to.meta.platformAdmin && !auth.user?.is_platform_admin) return { name: 'dashboard' }
  }
  return true
})

let announcementFrame: number | null = null

router.afterEach(async (to, from, failure) => {
  if (failure) return
  const title = typeof to.meta.title === 'string' ? to.meta.title : 'WorshipTool Sync'
  document.title = title === 'WorshipTool Sync' ? title : `${title} · WorshipTool Sync`
  await nextTick()

  const announcer = document.getElementById('route-announcer')
  if (announcer) {
    announcer.textContent = ''
    if (announcementFrame !== null) window.cancelAnimationFrame(announcementFrame)
    announcementFrame = window.requestAnimationFrame(() => {
      announcer.textContent = `${title} geladen`
      announcementFrame = null
    })
  }

  // Preserve native autofocus on the first page load. Subsequent SPA
  // navigations move focus to the new page heading so the view change is clear.
  if (from.name === undefined) return
  const target = [
    '#main-content h1',
    '.auth-card > header h2',
    '.onboarding-page h1',
    'main h1',
    '#main-content',
    'main',
  ].map((selector) => document.querySelector<HTMLElement>(selector)).find(Boolean)
  if (!target) return
  const temporaryTabIndex = !target.hasAttribute('tabindex')
  if (temporaryTabIndex) target.setAttribute('tabindex', '-1')
  target.focus({ preventScroll: true })
  if (temporaryTabIndex) {
    target.addEventListener('blur', () => target.removeAttribute('tabindex'), { once: true })
  }
})

export default router
