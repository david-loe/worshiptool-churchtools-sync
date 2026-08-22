<script setup lang="ts">
import OfflineBanner from '@/components/OfflineBanner.vue'
import PwaUpdate from '@/components/PwaUpdate.vue'
import ToastStack from '@/components/ToastStack.vue'
</script>

<template>
  <p id="route-announcer" class="sr-only" aria-live="polite" aria-atomic="true"></p>
  <OfflineBanner />
  <RouterView />
  <ToastStack />
  <PwaUpdate />
</template>
