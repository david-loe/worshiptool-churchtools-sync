<script setup lang="ts">
import { ref } from 'vue'
import AuthFrame from '@/components/AuthFrame.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import { api, errorMessage } from '@/api/client'

const email = ref('')
const loading = ref(false)
const error = ref<string | null>(null)
const sent = ref(false)

async function submit(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    await api.post<void>('/auth/recovery/request', { email: email.value.trim().toLowerCase() })
    sent.value = true
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <AuthFrame title="Passwort zurücksetzen" subtitle="Wir senden dir einen sicheren Link, falls ein Konto existiert.">
    <div v-if="sent" class="alert alert-success" role="status"><strong>E-Mail ist unterwegs.</strong><span>Bitte prüfe auch deinen Spam-Ordner. Der Link ist nur begrenzte Zeit gültig.</span></div>
    <template v-else>
      <ErrorBanner v-if="error" :message="error" />
      <form class="stack-form" @submit.prevent="submit">
        <label><span>E-Mail-Adresse</span><input v-model="email" type="email" autocomplete="email" required autofocus /></label>
        <button class="button button-primary button-wide" type="submit" :disabled="loading">{{ loading ? 'Wird gesendet …' : 'Link anfordern' }}</button>
      </form>
    </template>
    <p class="auth-switch"><RouterLink to="/login">Zurück zur Anmeldung</RouterLink></p>
  </AuthFrame>
</template>
