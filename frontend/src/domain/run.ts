import { ApiError } from '@/api/client'
import type { CreatedSongResult, RunEventResult, RunEventStatus } from '@/api/types'

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

const GERMAN_WEEKDAYS = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'] as const

export function eventDateLabel(startsAt: string): string {
  const match = startsAt.match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (!match) return 'unbekanntem Datum'
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const date = new Date(Date.UTC(year, month - 1, day))
  if (
    date.getUTCFullYear() !== year
    || date.getUTCMonth() !== month - 1
    || date.getUTCDate() !== day
  ) return 'unbekanntem Datum'
  return `${GERMAN_WEEKDAYS[date.getUTCDay()]} (${day}.${month}.)`
}

export function newSongShareText(song: CreatedSongResult, startsAt: string): string {
  const lines = [
    `*Neuer Song am ${eventDateLabel(startsAt)}*`,
    song.name,
    song.author,
  ]
  if (song.ccli) lines.push(`https://songselect.ccli.com/songs/${encodeURIComponent(song.ccli)}`)
  return lines.join('\n')
}

export function groupRunEvents(events: RunEventResult[]): Record<RunEventStatus, RunEventResult[]> {
  const grouped: Record<RunEventStatus, RunEventResult[]> = {
    failed: [],
    skipped: [],
    planned: [],
    verified: [],
  }
  for (const event of events) grouped[event.status].push(event)
  return grouped
}

export function runEventFromQuery(events: RunEventResult[], value: unknown): RunEventResult | null {
  if (typeof value !== 'string') return null
  return events.find((event) => event.id === value) ?? null
}
