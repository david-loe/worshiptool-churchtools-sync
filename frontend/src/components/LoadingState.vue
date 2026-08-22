<script setup lang="ts">
withDefaults(defineProps<{ label?: string }>(), { label: 'Wird geladen …' })
</script>

<template>
  <div class="loading-state" role="status">
    <span class="spinner" aria-hidden="true" />
    <span>{{ label }}</span>
  </div>
</template>
