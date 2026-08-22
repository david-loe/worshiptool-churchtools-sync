import type { RunStatus } from '@/api/types'

const dateTimeFormatter = new Intl.DateTimeFormat('de-DE', {
  dateStyle: 'medium',
  timeStyle: 'short',
})

const relativeFormatter = new Intl.RelativeTimeFormat('de-DE', { numeric: 'auto' })

export function formatDateTime(value?: string | null): string {
  if (!value) return '–'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '–' : dateTimeFormatter.format(date)
}

export function formatRelative(value?: string | null): string {
  if (!value) return '–'
  const diffSeconds = Math.round((new Date(value).getTime() - Date.now()) / 1_000)
  if (!Number.isFinite(diffSeconds)) return '–'
  if (Math.abs(diffSeconds) < 60) return relativeFormatter.format(diffSeconds, 'second')
  const minutes = Math.round(diffSeconds / 60)
  if (Math.abs(minutes) < 60) return relativeFormatter.format(minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 48) return relativeFormatter.format(hours, 'hour')
  return relativeFormatter.format(Math.round(hours / 24), 'day')
}

export function duration(start?: string | null, end?: string | null): string {
  if (!start) return '–'
  const milliseconds = (end ? new Date(end) : new Date()).getTime() - new Date(start).getTime()
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return '–'
  const seconds = Math.floor(milliseconds / 1_000)
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} min ${seconds % 60} s`
}

export const runStatusLabel: Record<RunStatus, string> = {
  queued: 'Eingereiht',
  running: 'Läuft',
  succeeded: 'Erfolgreich',
  partial: 'Teilweise',
  failed: 'Fehlgeschlagen',
  canceled: 'Abgebrochen',
  skipped: 'Übersprungen',
}

export function plural(count: number, singular: string, pluralForm = `${singular}e`): string {
  return `${count.toLocaleString('de-DE')} ${count === 1 ? singular : pluralForm}`
}
