<script setup lang="ts">
import { useToastStore } from '@/stores/toasts'

const toasts = useToastStore()
</script>

<template>
  <div class="toast-stack" aria-live="polite" aria-atomic="false">
    <article v-for="toast in toasts.items" :key="toast.id" class="toast" :class="`toast-${toast.kind}`">
      <div>
        <strong>{{ toast.title }}</strong>
        <p v-if="toast.message">{{ toast.message }}</p>
      </div>
      <button class="icon-button" type="button" aria-label="Hinweis schließen" @click="toasts.remove(toast.id)">×</button>
    </article>
  </div>
</template>
