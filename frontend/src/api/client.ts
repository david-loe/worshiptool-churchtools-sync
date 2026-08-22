import type { Page, ProblemDetails } from './types'
import { tenantCache } from './tenantCache'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api/v1').replace(/\/$/, '')
const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])
const MAX_AUTOMATIC_PAGES = 500
const MAX_AUTOMATIC_ITEMS = 50_000

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  ifMatch?: string
  workspaceId?: string
  cacheForMs?: number
}

export interface ApiResponse<T> {
  data: T
  etag?: string
  retryAfter?: number
}

export class ApiError extends Error {
  readonly status: number
  readonly problem: ProblemDetails
  readonly retryAfter?: number
  readonly headers: Headers
  readonly location?: string

  constructor(problem: ProblemDetails, retryAfter?: number, headers: HeadersInit = {}) {
    super(problem.detail || problem.title || `HTTP ${problem.status}`)
    this.name = 'ApiError'
    this.status = problem.status
    this.problem = problem
    this.retryAfter = retryAfter
    this.headers = new Headers(headers)
    this.location = this.headers.get('Location') ?? undefined
  }

  hasCode(code: string): boolean {
    return this.problem.code === code
  }
}

function readCookie(name: string): string | undefined {
  if (typeof document === 'undefined') return undefined
  const prefix = `${encodeURIComponent(name)}=`
  const part = document.cookie.split(';').map((item) => item.trim()).find((item) => item.startsWith(prefix))
  return part ? decodeURIComponent(part.slice(prefix.length)) : undefined
}

function parseRetryAfter(value: string | null): number | undefined {
  if (!value) return undefined
  const seconds = Number(value)
  if (Number.isFinite(seconds)) return Math.max(0, Math.ceil(seconds))
  const timestamp = Date.parse(value)
  return Number.isNaN(timestamp) ? undefined : Math.max(0, Math.ceil((timestamp - Date.now()) / 1000))
}

async function parseProblem(response: Response): Promise<ProblemDetails> {
  let payload: Partial<ProblemDetails> = {}
  try {
    payload = (await response.json()) as Partial<ProblemDetails>
  } catch {
    // Some gateways return HTML or an empty body. Keep the error safe and useful.
  }
  return {
    title: payload.title ?? response.statusText ?? 'Anfrage fehlgeschlagen',
    status: payload.status ?? response.status,
    detail: payload.detail,
    type: payload.type,
    instance: payload.instance,
    code: payload.code,
    trace_id: payload.trace_id,
    errors: payload.errors,
    run_id: payload.run_id,
  }
}

async function requestWithMeta<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
  const method = (options.method ?? 'GET').toUpperCase()
  const cacheKey = `${method}:${path}`
  if (method === 'GET' && options.workspaceId && options.cacheForMs) {
    const cached = tenantCache.get<ApiResponse<T>>(options.workspaceId, cacheKey)
    if (cached) return cached
  }

  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/json, application/problem+json')
  if (options.body !== undefined) headers.set('Content-Type', 'application/json')
  if (options.ifMatch) headers.set('If-Match', options.ifMatch)
  if (MUTATING_METHODS.has(method)) {
    const csrfToken = readCookie('wt_csrf')
    if (csrfToken) headers.set('X-CSRF-Token', csrfToken)
  }

  const generation = options.workspaceId ? tenantCache.token(options.workspaceId) : undefined
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    method,
    headers,
    credentials: 'same-origin',
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })
  const retryAfter = parseRetryAfter(response.headers.get('Retry-After'))
  if (!response.ok) throw new ApiError(await parseProblem(response), retryAfter, response.headers)

  const data = response.status === 204 ? undefined as T : await response.json() as T
  const result: ApiResponse<T> = { data, etag: response.headers.get('ETag') ?? undefined, retryAfter }
  if (method === 'GET' && options.workspaceId && options.cacheForMs && generation !== undefined
      && tenantCache.isTokenCurrent(options.workspaceId, generation)) {
    tenantCache.set(options.workspaceId, cacheKey, result, options.cacheForMs)
  }
  if (MUTATING_METHODS.has(method)) tenantCache.clear()
  return result
}

export const api = {
  activateWorkspace(workspaceId: string | null): void {
    tenantCache.activate(workspaceId)
  },
  clearCache(): void {
    tenantCache.clear()
  },
  async get<T>(path: string, options: RequestOptions = {}): Promise<T> {
    return (await requestWithMeta<T>(path, { ...options, method: 'GET' })).data
  },
  getWithMeta<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
    return requestWithMeta<T>(path, { ...options, method: 'GET' })
  },
  async post<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
    return (await requestWithMeta<T>(path, { ...options, method: 'POST', body })).data
  },
  async patch<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
    return (await requestWithMeta<T>(path, { ...options, method: 'PATCH', body })).data
  },
  async put<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
    return (await requestWithMeta<T>(path, { ...options, method: 'PUT', body })).data
  },
  async delete(path: string, options: RequestOptions = {}): Promise<void> {
    await requestWithMeta<void>(path, { ...options, method: 'DELETE' })
  },
  page<T>(path: string, options: RequestOptions = {}): Promise<Page<T>> {
    return this.get<Page<T>>(path, options)
  },
  async allPages<T>(path: string, options: RequestOptions = {}, pageSize = 200): Promise<T[]> {
    const normalizedPageSize = Math.min(200, Math.max(1, Math.trunc(pageSize)))
    const items: T[] = []
    let offset = 0
    let total = Number.POSITIVE_INFINITY
    let pages = 0
    while (offset < total) {
      pages += 1
      if (pages > MAX_AUTOMATIC_PAGES) throw new Error('Die Ressource umfasst zu viele Seiten für einen automatischen Abruf.')
      const page = await this.page<T>(pagePath(path, normalizedPageSize, offset), options)
      if (!Number.isSafeInteger(page.total) || page.total < 0
          || page.total > MAX_AUTOMATIC_ITEMS
          || Math.ceil(page.total / normalizedPageSize) > MAX_AUTOMATIC_PAGES) {
        throw new Error('Die Ressource überschreitet das sichere Limit für einen automatischen Abruf.')
      }
      total = page.total
      items.push(...page.items)
      if (items.length > MAX_AUTOMATIC_ITEMS) throw new Error('Die Ressource überschreitet das sichere Limit für einen automatischen Abruf.')
      if (!page.items.length) break
      offset += page.items.length
    }
    return items
  },
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof TypeError) return 'Der Server ist gerade nicht erreichbar. Bitte prüfe deine Verbindung.'
  if (error instanceof Error) return error.message
  return 'Ein unbekannter Fehler ist aufgetreten.'
}

export function pagePath(path: string, limit = 50, offset = 0): string {
  const separator = path.includes('?') ? '&' : '?'
  return `${path}${separator}limit=${limit}&offset=${offset}`
}
