<script setup lang="ts">
import { computed } from 'vue'
import type { RunStatus } from '@/api/types'
import { runStatusLabel } from '@/utils/format'

const props = defineProps<{ status: RunStatus | 'healthy' | 'error' | 'unknown' | 'degraded' | 'unavailable' }>()

const label = computed(() => {
  if (props.status in runStatusLabel) return runStatusLabel[props.status as RunStatus]
  const other = { healthy: 'Verbunden', error: 'Fehler', unknown: 'Ungeprüft', degraded: 'Eingeschränkt', unavailable: 'Nicht verfügbar' } as const
  return other[props.status as keyof typeof other]
})
</script>

<template>
  <span class="status-badge" :class="`status-${status}`">
    <span class="status-dot" aria-hidden="true" />{{ label }}
  </span>
</template>
