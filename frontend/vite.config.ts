import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    vue(),
    VitePWA({
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw.ts',
      registerType: 'prompt',
      injectRegister: false,
      manifest: {
        name: 'WorshipTool Sync',
        short_name: 'WT Sync',
        description: 'Sichere Synchronisation von WorshipTools und ChurchTools',
        theme_color: '#102a43',
        background_color: '#f3f7f8',
        display: 'standalone',
        start_url: '/',
        scope: '/',
        lang: 'de-DE',
        icons: [
          { src: '/pwa-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
          { src: '/pwa-512.png', sizes: '512x512', type: 'image/png', purpose: 'any maskable' },
          { src: '/pwa-512.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any' },
        ],
      },
      injectManifest: {
        globPatterns: ['**/*.{js,css,html,svg,png,webmanifest}'],
        // Manifest and declared icons are injected by vite-plugin-pwa separately.
        globIgnores: [
          '**/*.map',
          'pwa-192.png',
          'pwa-512.png',
          'pwa-192.svg',
          'pwa-512.svg',
          'manifest.webmanifest',
        ],
      },
      devOptions: { enabled: false },
    }),
  ],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': { target: process.env.VITE_DEV_API_URL ?? 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: {
    target: 'es2022',
    sourcemap: false,
    cssCodeSplit: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          return id.includes('node_modules') ? 'vendor' : undefined
        },
      },
    },
  },
})
