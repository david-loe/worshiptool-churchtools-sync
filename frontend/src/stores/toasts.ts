import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ToastKind = 'success' | 'error' | 'info' | 'warning'
export interface Toast {
  id: number
  kind: ToastKind
  title: string
  message?: string
}

export const useToastStore = defineStore('toasts', () => {
  const items = ref<Toast[]>([])
  let nextId = 1

  function show(kind: ToastKind, title: string, message?: string, timeoutMs = 5_000): void {
    const id = nextId++
    items.value.push({ id, kind, title, message })
    if (timeoutMs > 0) window.setTimeout(() => remove(id), timeoutMs)
  }

  function remove(id: number): void {
    items.value = items.value.filter((item) => item.id !== id)
  }

  return { items, show, remove }
})
