<script setup lang="ts">
defineProps<{ title: string; subtitle: string }>()
</script>

<template>
  <main class="auth-page">
    <section class="auth-intro" aria-label="Produktinformation">
      <div class="brand brand-light"><span class="brand-mark">W</span><span><strong>WorshipTool</strong><small>Sync</small></span></div>
      <div>
        <p class="eyebrow">Weniger Verwaltung. Mehr Musik.</p>
        <h1>Deine Abläufe, zuverlässig synchronisiert.</h1>
        <p>WorshipTools und ChurchTools bleiben automatisch auf demselben Stand – nachvollziehbar, sicher und für dein ganzes Team.</p>
      </div>
      <ul class="feature-list">
        <li><span>✓</span> Geplante und manuelle Synchronisation</li>
        <li><span>✓</span> Vollständige Historie statt Blackbox</li>
        <li><span>✓</span> Mandantentrennung und rollenbasierter Zugriff</li>
      </ul>
    </section>
    <section class="auth-panel">
      <div class="auth-card">
        <header><h2>{{ title }}</h2><p>{{ subtitle }}</p></header>
        <slot />
      </div>
    </section>
  </main>
</template>
