import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { useFocusTrap } from './useFocusTrap'

async function settleFocus(): Promise<void> {
  await nextTick()
  await nextTick()
}

describe('useFocusTrap', () => {
  it('moves focus inside, wraps Tab, closes on Escape and restores focus', async () => {
    const trigger = document.createElement('button')
    const host = document.createElement('div')
    document.body.append(trigger, host)
    trigger.focus()

    const active = ref(false)
    const panel = ref<HTMLElement | null>(null)
    const close = vi.fn(() => { active.value = false })
    const app = createApp(defineComponent({
      setup() {
        useFocusTrap(panel, () => active.value, { onEscape: close })
        return () => h('section', { ref: panel, tabindex: -1 }, [
          h('button', { id: 'first' }, 'Erste Aktion'),
          h('button', { id: 'last' }, 'Letzte Aktion'),
        ])
      },
    }))
    app.mount(host)

    active.value = true
    await settleFocus()
    expect(document.activeElement).toBe(panel.value)

    panel.value?.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Tab', shiftKey: true, bubbles: true, cancelable: true,
    }))
    expect(document.activeElement).toBe(host.querySelector('#last'))

    document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Tab', bubbles: true, cancelable: true,
    }))
    expect(document.activeElement).toBe(host.querySelector('#first'))

    document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape', bubbles: true, cancelable: true,
    }))
    await settleFocus()
    expect(close).toHaveBeenCalledOnce()
    expect(document.activeElement).toBe(trigger)

    app.unmount()
    host.remove()
    trigger.remove()
  })

  it('supports a dialog-specific initial focus target', async () => {
    const host = document.createElement('div')
    document.body.append(host)
    const active = ref(false)
    const panel = ref<HTMLElement | null>(null)
    const app = createApp(defineComponent({
      setup() {
        useFocusTrap(panel, () => active.value, {
          onEscape: () => { active.value = false },
          initialFocus: (container) => container.querySelector('input'),
        })
        return () => h('section', { ref: panel, tabindex: -1 }, [
          h('button', 'Schließen'),
          h('input', { 'aria-label': 'Name' }),
        ])
      },
    }))
    app.mount(host)

    active.value = true
    await settleFocus()
    expect(document.activeElement).toBe(host.querySelector('input'))

    app.unmount()
    host.remove()
  })
})
