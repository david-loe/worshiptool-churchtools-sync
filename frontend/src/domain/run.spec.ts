import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { eventDateLabel, groupRunEvents, newSongShareText, recoverableRunId, runEventFromQuery, runIdFromLocation } from './run'

describe('Run-Recovery', () => {
  it('akzeptiert nur Run-Ziele aus dem erwarteten Workspace', () => {
    expect(runIdFromLocation('/api/v1/workspaces/workspace-1/runs/run-1', 'workspace-1')).toBe('run-1')
    expect(runIdFromLocation('/api/v1/workspaces/workspace-2/runs/run-1', 'workspace-1')).toBeNull()
    expect(runIdFromLocation('https://attacker.invalid/runs/run-1', 'workspace-1')).toBeNull()
    expect(runIdFromLocation('https://attacker.invalid/api/v1/workspaces/workspace-1/runs/run-1', 'workspace-1')).toBeNull()
  })

  it('erkennt persistierte Runs bei Queue- und Aktiv-Konflikten', () => {
    for (const code of ['queue_unavailable', 'run_already_active']) {
      const error = new ApiError(
        { title: 'Run vorhanden', status: code === 'queue_unavailable' ? 503 : 409, code },
        undefined,
        { Location: '/api/v1/workspaces/workspace-1/runs/run-1' },
      )
      expect(recoverableRunId(error, 'workspace-1')).toBe('run-1')
    }
  })
})

describe('Teilbarer Songtext', () => {
  const song = {
    action_id: 'action-1',
    source_song_id: 'source-1',
    target_song_id: '123',
    name: 'Songtitel',
    author: 'Autor',
    ccli: '12345',
  }

  it('formatiert Wochentag, Datum und SongSelect-Link exakt', () => {
    expect(eventDateLabel('2026-09-06T10:00:00+02:00')).toBe('So (6.9.)')
    expect(newSongShareText(song, '2026-09-06T10:00:00+02:00')).toBe(
      '*Neuer Song am So (6.9.)*\nSongtitel\nAutor\nhttps://songselect.ccli.com/songs/12345',
    )
  })

  it('lässt bei fehlender CCLI ausschließlich die SongSelect-Zeile weg', () => {
    expect(newSongShareText({ ...song, ccli: null }, '2026-09-06T10:00:00+02:00')).toBe(
      '*Neuer Song am So (6.9.)*\nSongtitel\nAutor',
    )
  })
})

describe('Ereignisergebnisse', () => {
  const baseEvent = {
    source_event_id: 'source',
    target_event_id: 'target',
    source_event_name: 'Service',
    source_event_starts_at: ['2026-09-06T08:00:00Z'],
    target_event_name: 'Gottesdienst',
    target_event_starts_at: '2026-09-06T08:00:00Z',
    messages: [],
    action_counts: { planned: 0, applied: 0, verified: 0, skipped: 0, failed: 0 },
    action_total: 0,
    new_songs: [],
  }
  const events = [
    { ...baseEvent, id: 'failed-event', status: 'failed' as const },
    { ...baseEvent, id: 'verified-event', status: 'verified' as const },
  ]

  it('gruppiert Ergebnisse deterministisch nach Endstatus', () => {
    const grouped = groupRunEvents(events)
    expect(grouped.failed.map((event) => event.id)).toEqual(['failed-event'])
    expect(grouped.verified.map((event) => event.id)).toEqual(['verified-event'])
    expect(grouped.skipped).toEqual([])
    expect(grouped.planned).toEqual([])
  })

  it('findet das per Deep-Link angeforderte Ereignis', () => {
    expect(runEventFromQuery(events, 'verified-event')?.status).toBe('verified')
    expect(runEventFromQuery(events, ['verified-event'])).toBeNull()
    expect(runEventFromQuery(events, 'unknown')).toBeNull()
  })
})
