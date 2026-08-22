<script setup lang="ts">
withDefaults(defineProps<{ title: string; text?: string; symbol?: string }>(), { text: '', symbol: '○' })
</script>

<template>
  <div class="empty-state">
    <span class="empty-symbol" aria-hidden="true">{{ symbol }}</span>
    <h2>{{ title }}</h2>
    <p v-if="text">{{ text }}</p>
    <div v-if="$slots.default" class="empty-actions"><slot /></div>
  </div>
</template>
