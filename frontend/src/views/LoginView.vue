<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AuthFrame from '@/components/AuthFrame.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import { ApiError, errorMessage } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspaces'

const auth = useAuthStore()
const workspaces = useWorkspaceStore()
const router = useRouter()
const route = useRoute()
const email = ref('')
const password = ref('')
const totpCode = ref('')
const recoveryCode = ref('')
const totpRequired = ref(false)
const useRecoveryCode = ref(false)
const loading = ref(false)
const error = ref<string | null>(null)

async function submit(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const loggedInUser = await auth.login(
      email.value,
      password.value,
      useRecoveryCode.value ? undefined : totpCode.value || undefined,
      useRecoveryCode.value ? recoveryCode.value || undefined : undefined,
    )
    await workspaces.load()
    const redirect = typeof route.query.redirect === 'string'
      && route.query.redirect.startsWith('/')
      && !route.query.redirect.startsWith('//')
      ? route.query.redirect
      : '/dashboard'
    await router.push(workspaces.activeId ? redirect : loggedInUser.is_platform_admin ? '/account' : '/onboarding')
  } catch (cause) {
    if (cause instanceof ApiError && (cause.hasCode('mfa_required') || cause.hasCode('totp_required'))) {
      totpRequired.value = true
      error.value = 'Bitte gib zusätzlich den Code aus deiner Authenticator-App ein.'
    } else {
      error.value = errorMessage(cause)
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <AuthFrame title="Willkommen zurück" subtitle="Melde dich an, um deine Synchronisationen zu verwalten.">
    <div v-if="route.query.registriert" class="alert alert-success" role="status"><strong>Konto wurde erstellt.</strong><span>Bestätige gegebenenfalls zuerst den Link in deiner E-Mail und melde dich dann an.</span></div>
    <ErrorBanner v-if="error" :message="error" />
    <form class="stack-form" @submit.prevent="submit">
      <label><span>E-Mail-Adresse</span><input v-model="email" type="email" autocomplete="email" required autofocus placeholder="du@gemeinde.de" /></label>
      <label><span>Passwort</span><input v-model="password" type="password" autocomplete="current-password" minlength="12" required /></label>
      <label v-if="totpRequired && !useRecoveryCode"><span>Bestätigungscode</span><input v-model="totpCode" class="code-input" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required placeholder="000000" /></label>
      <label v-else-if="totpRequired"><span>Wiederherstellungscode</span><input v-model="recoveryCode" autocomplete="one-time-code" required placeholder="xxxxxxxx-xxxxxxxx" /></label>
      <button v-if="totpRequired" class="link-button" type="button" @click="useRecoveryCode = !useRecoveryCode">{{ useRecoveryCode ? 'Authenticator-Code verwenden' : 'Wiederherstellungscode verwenden' }}</button>
      <div class="form-row between"><span /><RouterLink to="/passwort-vergessen">Passwort vergessen?</RouterLink></div>
      <button class="button button-primary button-wide" type="submit" :disabled="loading">{{ loading ? 'Anmeldung läuft …' : 'Anmelden' }}</button>
    </form>
    <p class="auth-switch">Noch kein Konto? <RouterLink to="/registrieren">Jetzt registrieren</RouterLink></p>
  </AuthFrame>
</template>
