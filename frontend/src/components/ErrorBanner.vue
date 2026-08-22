<script setup lang="ts">
defineProps<{ message: string; traceId?: string }>()
</script>

<template>
  <div class="alert alert-error" role="alert">
    <strong>Das hat nicht geklappt.</strong>
    <span>{{ message }}</span>
    <small v-if="traceId">Referenz: {{ traceId }}</small>
  </div>
</template>
