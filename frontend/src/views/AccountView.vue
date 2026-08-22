<script setup lang="ts">
import { ref } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import ErrorBanner from '@/components/ErrorBanner.vue'
import type { TotpSetup } from '@/api/types'
import { errorMessage } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useToastStore } from '@/stores/toasts'

const auth = useAuthStore()
const toasts = useToastStore()
const setup = ref<TotpSetup | null>(null)
const code = ref('')
const password = ref('')
const recoveryCode = ref('')
const useRecoveryCode = ref(false)
const recoveryCodes = ref<string[]>([])
const error = ref<string | null>(null)
const loading = ref(false)

async function startTotp(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    setup.value = await auth.startTotp(
      password.value,
      auth.user?.totp_enabled && !useRecoveryCode.value ? code.value : undefined,
      auth.user?.totp_enabled && useRecoveryCode.value ? recoveryCode.value : undefined,
    )
    password.value = ''
    code.value = ''
    recoveryCode.value = ''
  } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false }
}

async function confirmTotp(): Promise<void> {
  loading.value = true
  error.value = null
  try { recoveryCodes.value = await auth.confirmTotp(code.value); setup.value = null; code.value = ''; toasts.show('success', 'Zwei-Faktor-Anmeldung aktiviert') } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false }
}

async function disableTotp(): Promise<void> {
  if (!confirm('Zwei-Faktor-Anmeldung wirklich deaktivieren?')) return
  loading.value = true
  error.value = null
  try {
    await auth.disableTotp(
      password.value,
      useRecoveryCode.value ? undefined : code.value,
      useRecoveryCode.value ? recoveryCode.value : undefined,
    )
    password.value = ''
    code.value = ''
    recoveryCode.value = ''
    toasts.show('success', 'Zwei-Faktor-Anmeldung deaktiviert')
  } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false }
}

function downloadCodes(): void {
  const blob = new Blob([recoveryCodes.value.join('\n')], { type: 'text/plain' })
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = 'worshiptool-sync-wiederherstellungscodes.txt'
  link.click()
  URL.revokeObjectURL(link.href)
}
</script>

<template>
  <PageHeader title="Mein Konto" eyebrow="Sicherheit" description="Schütze deinen Zugang mit einem zweiten Faktor und sicheren Wiederherstellungscodes." />
  <ErrorBanner v-if="error" :message="error" />
  <div class="account-grid"><section class="card account-card"><div class="section-heading"><div><h2>Kontodaten</h2><p>Deine Identität auf dieser Instanz</p></div></div><dl class="account-data"><div><dt>E-Mail-Adresse</dt><dd>{{ auth.user?.email }}</dd></div><div><dt>E-Mail bestätigt</dt><dd>{{ auth.user?.email_verified_at ? 'Ja' : 'Noch nicht' }}</dd></div><div><dt>Plattform-Administrator</dt><dd>{{ auth.user?.is_platform_admin ? 'Ja' : 'Nein' }}</dd></div></dl></section>
    <section class="card account-card"><div class="section-heading"><div><h2>Zwei-Faktor-Anmeldung (TOTP)</h2><p>Code aus einer Authenticator-App beim Login</p></div><span class="security-state" :class="{ enabled: auth.user?.totp_enabled }">{{ auth.user?.totp_enabled ? 'Aktiv' : 'Inaktiv' }}</span></div>
      <div v-if="recoveryCodes.length" class="recovery-codes"><div class="alert alert-warning"><strong>Einmalig anzeigen und sicher speichern</strong><span>Jeder Code kann nur einmal verwendet werden.</span></div><code v-for="item in recoveryCodes" :key="item">{{ item }}</code><button class="button button-secondary" type="button" @click="downloadCodes">Als Textdatei speichern</button></div>
      <form v-else-if="setup" class="stack-form" @submit.prevent="confirmTotp"><p>Gib den Schlüssel manuell ein oder importiere die Provisioning-URI in deine Authenticator-App.</p><label><span>Einrichtungsschlüssel</span><input :value="setup.secret" readonly /></label><details><summary>Provisioning-URI anzeigen</summary><code class="break-code">{{ setup.provisioning_uri }}</code></details><label><span>6-stelliger Code</span><input v-model="code" class="code-input" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label><button class="button button-primary" type="submit" :disabled="loading">Aktivierung bestätigen</button></form>
      <form v-else-if="auth.user?.totp_enabled" class="stack-form" @submit.prevent="startTotp"><p>Ersetze den Authenticator oder deaktiviere TOTP mit Passwort und einem aktuellen Faktor.</p><label><span>Passwort</span><input v-model="password" type="password" autocomplete="current-password" required /></label><label v-if="!useRecoveryCode"><span>Aktueller TOTP-Code</span><input v-model="code" class="code-input" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label><label v-else><span>Wiederherstellungscode</span><input v-model="recoveryCode" autocomplete="one-time-code" maxlength="64" required /></label><button class="link-button" type="button" @click="useRecoveryCode = !useRecoveryCode">{{ useRecoveryCode ? 'Authenticator-Code verwenden' : 'Wiederherstellungscode verwenden' }}</button><div class="dialog-actions"><button class="button button-secondary" type="submit" :disabled="loading">Authenticator ersetzen</button><button class="button button-danger" type="button" :disabled="loading || auth.user?.is_platform_admin" @click="disableTotp">Deaktivieren</button></div><small v-if="auth.user?.is_platform_admin">Für Plattform-Administratoren ist TOTP verpflichtend; ein sicher bestätigter Austausch ist möglich.</small></form>
      <form v-else class="stack-form" @submit.prevent="startTotp"><p>Ein zweiter Faktor verhindert Kontoübernahmen selbst dann, wenn dein Passwort bekannt wird.</p><label><span>Passwort bestätigen</span><input v-model="password" type="password" autocomplete="current-password" required /></label><button class="button button-primary" type="submit" :disabled="loading">TOTP einrichten</button></form>
    </section></div>
</template>
