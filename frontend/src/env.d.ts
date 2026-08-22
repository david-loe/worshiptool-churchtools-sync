/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

// vite-plugin-pwa exposes optional asset-generator types even when that heavy
// image toolchain is intentionally not installed by applications with ready-made icons.
declare module '@vite-pwa/assets-generator/api' {
  export type ImageAssetsInstructions = unknown
  export type IconAsset<T = unknown> = T & { path?: string }
  export type FaviconLink = Record<string, unknown>
  export type HtmlLink = Record<string, unknown>
  export type AppleSplashScreenLink = Record<string, unknown>
  export type HtmlLinkPreset = Record<string, unknown>
}

declare module '@vite-pwa/assets-generator/config' {
  export type BuiltInPreset = string
  export type Preset = Record<string, unknown>
}
