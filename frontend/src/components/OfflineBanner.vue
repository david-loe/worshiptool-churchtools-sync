<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

const offline = ref(typeof navigator !== 'undefined' && !navigator.onLine)
const update = (): void => { offline.value = !navigator.onLine }
onMounted(() => {
  window.addEventListener('online', update)
  window.addEventListener('offline', update)
})
onBeforeUnmount(() => {
  window.removeEventListener('online', update)
  window.removeEventListener('offline', update)
})
</script>

<template>
  <div v-if="offline" class="offline-banner" role="status">
    Du bist offline. Einstellungen und Synchronisationsdaten sind erst wieder online verfügbar.
  </div>
</template>
