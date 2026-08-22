<script setup lang="ts">
import { useRegisterSW } from 'virtual:pwa-register/vue'

const { needRefresh, offlineReady, updateServiceWorker } = useRegisterSW({ immediate: true })
</script>

<template>
  <div v-if="needRefresh || offlineReady" class="pwa-prompt" role="status">
    <span>{{ needRefresh ? 'Eine neue Version ist verfügbar.' : 'Die App ist für den Offline-Start bereit.' }}</span>
    <button v-if="needRefresh" class="button button-small" type="button" @click="updateServiceWorker(true)">Aktualisieren</button>
    <button class="icon-button" type="button" aria-label="Hinweis schließen" @click="needRefresh = false; offlineReady = false">×</button>
  </div>
</template>
