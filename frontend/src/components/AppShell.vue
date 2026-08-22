<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useFocusTrap } from '@/composables/useFocusTrap'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspaces'

const auth = useAuthStore()
const workspaceStore = useWorkspaceStore()
const router = useRouter()
const route = useRoute()
const menuOpen = ref(false)
const navigationPanel = ref<HTMLElement | null>(null)

useFocusTrap(navigationPanel, () => menuOpen.value, {
  onEscape: () => { menuOpen.value = false },
})

const navigation = computed(() => [
  { to: '/dashboard', label: 'Übersicht', symbol: '⌂' },
  { to: '/profiles', label: 'Sync-Profile', symbol: '⇄' },
  { to: '/connections', label: 'Verbindungen', symbol: '⌁', manage: true },
  { to: '/runs', label: 'Historie', symbol: '◷' },
  { to: '/notifications', label: 'Benachrichtigungen', symbol: '◉' },
  { to: '/workspaces', label: 'Workspace & Team', symbol: '♙', manage: true },
].filter((item) => !item.manage || workspaceStore.canManage))

function switchWorkspace(event: Event): void {
  const id = (event.target as HTMLSelectElement).value
  workspaceStore.select(id)
  menuOpen.value = false
  void router.push({ path: '/dashboard', query: { workspace: id } })
}

async function logout(): Promise<void> {
  await auth.logout()
  workspaceStore.reset()
  await router.push('/login')
}
</script>

<template>
  <div class="app-shell">
    <a class="skip-link" href="#main-content">Zum Inhalt springen</a>
    <header class="mobile-header">
      <button class="icon-button menu-button" type="button" :aria-expanded="menuOpen" aria-controls="main-navigation" :aria-label="menuOpen ? 'Navigation schließen' : 'Navigation öffnen'" @click="menuOpen = !menuOpen">☰</button>
      <RouterLink class="mobile-brand" to="/dashboard"><span class="brand-mark">W</span><strong>WT Sync</strong></RouterLink>
      <RouterLink class="icon-button" to="/notifications" aria-label="Benachrichtigungen">◉</RouterLink>
    </header>

    <aside id="main-navigation" ref="navigationPanel" class="sidebar" :class="{ open: menuOpen }" aria-label="Seitennavigation" tabindex="-1">
      <RouterLink class="brand" to="/dashboard" @click="menuOpen = false">
        <span class="brand-mark">W</span>
        <span><strong>WorshipTool</strong><small>Sync</small></span>
      </RouterLink>

      <label class="workspace-picker">
        <span>Workspace</span>
        <select :value="workspaceStore.activeId ?? ''" @change="switchWorkspace">
          <option v-for="workspace in workspaceStore.workspaces" :key="workspace.id" :value="workspace.id">{{ workspace.name }}</option>
        </select>
      </label>

      <nav aria-label="Hauptnavigation">
        <RouterLink v-for="item in navigation" :key="item.to" :to="item.to" @click="menuOpen = false">
          <span class="nav-symbol" aria-hidden="true">{{ item.symbol }}</span>{{ item.label }}
        </RouterLink>
      </nav>

      <div class="sidebar-footer">
        <RouterLink to="/account" @click="menuOpen = false"><span class="avatar" aria-hidden="true">{{ auth.user?.email.slice(0, 1).toUpperCase() }}</span><span><strong>Mein Konto</strong><small>{{ auth.user?.email }}</small></span></RouterLink>
        <RouterLink v-if="auth.user?.is_platform_admin" to="/system" @click="menuOpen = false">Systemverwaltung</RouterLink>
        <button type="button" @click="logout">Abmelden</button>
      </div>
    </aside>
    <div v-if="menuOpen" class="menu-backdrop" aria-hidden="true" @click="menuOpen = false" />

    <main id="main-content" class="main-content" tabindex="-1">
      <RouterView :key="`${workspaceStore.activeId}:${route.fullPath}`" />
    </main>
  </div>
</template>
