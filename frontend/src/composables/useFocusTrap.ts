import { nextTick, onBeforeUnmount, watch, type Ref } from 'vue'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'details > summary:first-of-type',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

interface FocusTrapOptions {
  onEscape: () => void
  initialFocus?: (container: HTMLElement) => HTMLElement | null
}

function visibleFocusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter((element) => {
    const style = window.getComputedStyle(element)
    return style.display !== 'none' && style.visibility !== 'hidden' && !element.closest('[inert]')
  })
}

function focusWithoutScroll(element: HTMLElement): void {
  element.focus({ preventScroll: true })
}

export function useFocusTrap(
  container: Ref<HTMLElement | null>,
  active: () => boolean,
  options: FocusTrapOptions,
): void {
  let previousFocus: HTMLElement | null = null
  let generation = 0

  const handleKeydown = (event: KeyboardEvent): void => {
    if (!active() || !container.value) return
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      options.onEscape()
      return
    }
    if (event.key !== 'Tab') return

    const focusable = visibleFocusableElements(container.value)
    if (!focusable.length) {
      event.preventDefault()
      focusWithoutScroll(container.value)
      return
    }
    const first = focusable[0]!
    const last = focusable[focusable.length - 1]!
    const focused = document.activeElement
    if (event.shiftKey && (focused === container.value || focused === first || !container.value.contains(focused))) {
      event.preventDefault()
      focusWithoutScroll(last)
    } else if (!event.shiftKey && (focused === container.value || focused === last || !container.value.contains(focused))) {
      event.preventDefault()
      focusWithoutScroll(first)
    }
  }

  watch(active, async (isActive) => {
    generation += 1
    const currentGeneration = generation
    if (isActive) {
      previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
      document.addEventListener('keydown', handleKeydown, true)
      await nextTick()
      if (currentGeneration !== generation || !active() || !container.value) return
      const initial = options.initialFocus?.(container.value) ?? container.value
      focusWithoutScroll(initial)
      return
    }

    document.removeEventListener('keydown', handleKeydown, true)
    const target = previousFocus
    previousFocus = null
    await nextTick()
    if (currentGeneration === generation && target?.isConnected) focusWithoutScroll(target)
  }, { flush: 'post' })

  onBeforeUnmount(() => {
    generation += 1
    document.removeEventListener('keydown', handleKeydown, true)
  })
}
