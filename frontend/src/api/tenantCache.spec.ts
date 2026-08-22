import { describe, expect, it, vi } from 'vitest'
import { TenantCache } from './tenantCache'

describe('TenantCache', () => {
  it('verwirft alle Werte beim Workspace-Wechsel', () => {
    const cache = new TenantCache()
    cache.activate('workspace-a')
    cache.set('workspace-a', '/profiles', { secret: 'nur-a' }, 1_000)

    cache.activate('workspace-b')

    expect(cache.get('workspace-a', '/profiles')).toBeUndefined()
    expect(cache.get('workspace-b', '/profiles')).toBeUndefined()
  })

  it('ignoriert verspätete Antworten aus dem vorherigen Workspace', () => {
    const cache = new TenantCache()
    cache.activate('workspace-a')
    const token = cache.token('workspace-a')
    cache.activate('workspace-b')

    expect(cache.isTokenCurrent('workspace-a', token)).toBe(false)
  })

  it('liefert abgelaufene Einträge nicht aus', () => {
    vi.spyOn(Date, 'now').mockReturnValueOnce(100).mockReturnValue(201)
    const cache = new TenantCache()
    cache.activate('workspace-a')
    cache.set('workspace-a', 'key', 'value', 100)

    expect(cache.get('workspace-a', 'key')).toBeUndefined()
  })
})
