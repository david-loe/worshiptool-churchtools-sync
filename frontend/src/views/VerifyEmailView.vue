<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AuthFrame from '@/components/AuthFrame.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import LoadingState from '@/components/LoadingState.vue'
import { api, errorMessage } from '@/api/client'
import type { VerificationRequestedResponse } from '@/api/types'

const route = useRoute()
const router = useRouter()
const loading = ref(true)
const verified = ref(false)
const error = ref<string | null>(null)
const email = ref(typeof route.query.email === 'string' ? route.query.email : '')
const resendLoading = ref(false)
const resendAccepted = ref(route.query.sent === '1')

async function verify(token: string): Promise<void> {
  try {
    await api.post<void>('/auth/verify-email', { token })
    verified.value = true
    error.value = null
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function requestAgain(): Promise<void> {
  resendLoading.value = true
  error.value = null
  try {
    const response = await api.post<VerificationRequestedResponse>(
      '/auth/verification/request',
      { email: email.value.trim().toLowerCase() },
    )
    resendAccepted.value = response.accepted
    if (response.development_verification_token) {
      await verify(response.development_verification_token)
    }
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    resendLoading.value = false
  }
}

onMounted(async () => {
  const token = typeof route.query.token === 'string' ? route.query.token : ''
  if (!token) {
    loading.value = false
    return
  }
  await router.replace({ path: route.path, query: email.value ? { email: email.value } : {} })
  await verify(token)
  loading.value = false
})
</script>

<template>
  <AuthFrame title="E-Mail bestätigen" subtitle="Damit schützen wir dein Konto und deine Benachrichtigungen.">
    <LoadingState v-if="loading" label="E-Mail wird bestätigt …" />
    <template v-else>
      <ErrorBanner v-if="error" :message="error" />
      <div v-if="verified" class="alert alert-success" role="status"><strong>E-Mail bestätigt.</strong><span>Dein Konto ist jetzt vollständig aktiviert.</span></div>
      <template v-else>
        <div v-if="resendAccepted" class="alert alert-success" role="status"><strong>Nachricht angefordert.</strong><span>Falls ein noch unbestätigtes Konto existiert, wurde ein neuer Link versendet. Prüfe auch den Spam-Ordner.</span></div>
        <form class="stack-form" @submit.prevent="requestAgain">
          <label><span>E-Mail-Adresse</span><input v-model="email" type="email" autocomplete="email" required /></label>
          <button class="button button-primary button-wide" type="submit" :disabled="resendLoading">{{ resendLoading ? 'Link wird angefordert …' : 'Bestätigungslink erneut senden' }}</button>
        </form>
      </template>
    </template>
    <p class="auth-switch"><RouterLink to="/login">Weiter zur Anmeldung</RouterLink></p>
  </AuthFrame>
</template>
