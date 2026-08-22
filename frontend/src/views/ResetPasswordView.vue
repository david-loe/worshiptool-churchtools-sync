<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AuthFrame from '@/components/AuthFrame.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import { api, errorMessage } from '@/api/client'

const route = useRoute()
const router = useRouter()
const token = ref(typeof route.query.token === 'string' ? route.query.token : '')
const password = ref('')
const repeated = ref('')
const loading = ref(false)
const done = ref(false)
const error = ref<string | null>(token.value ? null : 'Der Wiederherstellungslink ist unvollständig.')

onMounted(() => {
  if (token.value) void router.replace({ path: route.path, query: {} })
})

async function submit(): Promise<void> {
  if (password.value !== repeated.value) { error.value = 'Die Passwörter stimmen nicht überein.'; return }
  loading.value = true
  try { await api.post<void>('/auth/recovery/confirm', { token: token.value, new_password: password.value }); done.value = true } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false }
}
</script>

<template><AuthFrame title="Neues Passwort" subtitle="Der Link ist nur einmal und für begrenzte Zeit gültig."><div v-if="done" class="alert alert-success"><strong>Passwort geändert.</strong><span>Alle bestehenden Sitzungen wurden aus Sicherheitsgründen beendet.</span></div><template v-else><ErrorBanner v-if="error" :message="error" /><form class="stack-form" @submit.prevent="submit"><label><span>Neues Passwort</span><input v-model="password" type="password" autocomplete="new-password" minlength="12" required /></label><label><span>Passwort wiederholen</span><input v-model="repeated" type="password" autocomplete="new-password" required /></label><button class="button button-primary button-wide" type="submit" :disabled="loading || !token">{{ loading ? 'Wird geändert …' : 'Passwort ändern' }}</button></form></template><p class="auth-switch"><RouterLink to="/login">Zur Anmeldung</RouterLink></p></AuthFrame></template>
