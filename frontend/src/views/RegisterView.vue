<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import AuthFrame from '@/components/AuthFrame.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import { errorMessage } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const email = ref('')
const workspaceName = ref('')
const password = ref('')
const passwordRepeat = ref('')
const accepted = ref(false)
const loading = ref(false)
const error = ref<string | null>(null)
const passwordChecks = computed(() => [
  { label: 'mindestens 12 Zeichen', ok: password.value.length >= 12 },
  { label: 'Groß- und Kleinbuchstaben', ok: /[a-z]/.test(password.value) && /[A-Z]/.test(password.value) },
  { label: 'Zahl oder Sonderzeichen', ok: /[^A-Za-z]/.test(password.value) },
])

async function submit(): Promise<void> {
  if (password.value !== passwordRepeat.value) {
    error.value = 'Die Passwörter stimmen nicht überein.'
    return
  }
  if (!passwordChecks.value.every((check) => check.ok)) {
    error.value = 'Das Passwort erfüllt noch nicht alle Anforderungen.'
    return
  }
  loading.value = true
  error.value = null
  try {
    const result = await auth.register(email.value, password.value, workspaceName.value)
    if (result.verification_required) {
      await router.push({
        path: '/email-bestaetigen',
        query: {
          email: email.value.trim().toLowerCase(),
          sent: '1',
          ...(result.development_verification_token
            ? { token: result.development_verification_token }
            : {}),
        },
      })
    } else {
      await router.push('/login?registriert=1')
    }
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <AuthFrame title="Konto erstellen" subtitle="Starte mit einem sicheren Workspace für deine Gemeinde.">
    <ErrorBanner v-if="error" :message="error" />
    <form class="stack-form" @submit.prevent="submit">
      <label><span>E-Mail-Adresse</span><input v-model="email" type="email" autocomplete="email" required autofocus placeholder="du@gemeinde.de" /></label>
      <label><span>Name der Gemeinde / Workspace</span><input v-model="workspaceName" autocomplete="organization" required placeholder="Meine Gemeinde" /></label>
      <label><span>Passwort</span><input v-model="password" type="password" autocomplete="new-password" minlength="12" required /></label>
      <ul class="password-checks" aria-label="Passwortanforderungen">
        <li v-for="check in passwordChecks" :key="check.label" :class="{ valid: check.ok }"><span aria-hidden="true">{{ check.ok ? '✓' : '○' }}</span> {{ check.label }}</li>
      </ul>
      <label><span>Passwort wiederholen</span><input v-model="passwordRepeat" type="password" autocomplete="new-password" required /></label>
      <label class="check-label"><input v-model="accepted" type="checkbox" required /> <span>Ich akzeptiere die Nutzungs- und Datenschutzbestimmungen.</span></label>
      <button class="button button-primary button-wide" type="submit" :disabled="loading || !accepted">{{ loading ? 'Konto wird erstellt …' : 'Kostenlos registrieren' }}</button>
    </form>
    <p class="auth-switch">Schon registriert? <RouterLink to="/login">Zur Anmeldung</RouterLink></p>
  </AuthFrame>
</template>
