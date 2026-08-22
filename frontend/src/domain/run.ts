import { ApiError } from '@/api/client'

const RECOVERABLE_RUN_CODES = new Set(['queue_unavailable', 'run_already_active'])

export function runIdFromLocation(location: string | undefined, workspaceId: string): string | null {
  if (!location) return null
  let pathname: string
  try {
    const baseOrigin = typeof window === 'undefined' ? 'https://local.invalid' : window.location.origin
    const parsed = new URL(location, baseOrigin)
    if (parsed.origin !== baseOrigin) return null
    pathname = parsed.pathname
  } catch {
    return null
  }
  const match = pathname.match(/\/workspaces\/([^/]+)\/runs\/([^/]+)\/?$/)
  if (!match) return null
  try {
    if (decodeURIComponent(match[1]!) !== workspaceId) return null
    return decodeURIComponent(match[2]!)
  } catch {
    return null
  }
}

export function recoverableRunId(error: unknown, workspaceId: string): string | null {
  if (!(error instanceof ApiError) || !RECOVERABLE_RUN_CODES.has(error.problem.code ?? '')) return null
  return runIdFromLocation(error.location, workspaceId)
}
