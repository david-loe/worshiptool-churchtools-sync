import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './client'

describe('API-Client', () => {
  beforeEach(() => {
    api.activateWorkspace(null)
    document.cookie = 'wt_csrf=csrf-test; path=/'
  })

  it('sendet Session-Credentials und CSRF bei Mutationen', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ id: '1' }), {
      status: 201,
      headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await api.post('/workspaces', { name: 'Test' })

    const [, init] = fetchMock.mock.calls[0]!
    expect(init?.credentials).toBe('same-origin')
    expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe('csrf-test')
    expect(init?.body).toBe(JSON.stringify({ name: 'Test' }))
  })

  it('übersetzt RFC-9457-Fehler und Retry-After', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      title: 'Manueller Start zu früh',
      status: 429,
      detail: 'Bitte später erneut versuchen.',
      code: 'manual_run_cooldown',
      trace_id: 'trace-1',
    }), { status: 429, headers: { 'Content-Type': 'application/problem+json', 'Retry-After': '121' } })))

    const error = await api.post('/workspaces/w/profiles/p/runs', { dry_run: false }).catch((cause: unknown) => cause)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 429, retryAfter: 121 })
    expect((error as ApiError).hasCode('manual_run_cooldown')).toBe(true)
  })

  it('übernimmt exponierte Fehler-Header einschließlich Location', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      title: 'Queue nicht verfügbar',
      status: 503,
      detail: 'Der Lauf wurde gespeichert.',
      code: 'queue_unavailable',
    }), {
      status: 503,
      headers: {
        'Content-Type': 'application/problem+json',
        Location: '/api/v1/workspaces/workspace-1/runs/run-1',
        'X-Request-ID': 'request-1',
      },
    })))

    const error = await api.post('/workspaces/workspace-1/profiles/profile-1/runs', { dry_run: false }).catch((cause: unknown) => cause)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ location: '/api/v1/workspaces/workspace-1/runs/run-1' })
    expect((error as ApiError).headers.get('X-Request-ID')).toBe('request-1')
  })

  it('sendet Änderungen ohne bedingten If-Match-Header', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ revision: 3 }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await api.patch('/workspaces/w/profiles/p', { enabled: true })

    expect(new Headers(fetchMock.mock.calls[0]![1]?.headers).has('If-Match')).toBe(false)
  })

  it('lädt paginierte Ressourcen vollständig in begrenzten Seiten', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ id: '1' }, { id: '2' }], total: 3, limit: 2, offset: 0 }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ id: '3' }], total: 3, limit: 2, offset: 2 }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await api.allPages<{ id: string }>('/workspaces', {}, 2)

    expect(result.map((item) => item.id)).toEqual(['1', '2', '3'])
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      '/api/v1/workspaces?limit=2&offset=0',
      '/api/v1/workspaces?limit=2&offset=2',
    ])
  })

  it('bricht bei einem unbeschränkten Pagination-Total sofort ab', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      items: [{ id: '1' }],
      total: 50_001,
      limit: 200,
      offset: 0,
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.allPages('/workspaces')).rejects.toThrow('sichere Limit')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
