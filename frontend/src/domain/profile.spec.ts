import { describe, expect, it } from 'vitest'
import { reactive } from 'vue'
import type { ProviderMetadata, SyncProfile, SyncProfileInput } from '@/api/types'
import { describeSongSelection, newProfile, profileInputFromProfile, sanitizeProfile } from './profile'

describe('Profilmodell', () => {
  it.each([
    [0, null, 'Alle Songs werden verwendet.'],
    [0, -1, 'Alle Songs außer dem letzten werden verwendet.'],
    [-1, null, 'Nur der letzte Song wird verwendet.'],
    [0, -3, 'Alle Songs außer den letzten 3 Songs werden verwendet.'],
    [-3, null, 'Die letzten 3 Songs werden verwendet.'],
    [1, 4, 'Songs 2 bis 4 werden verwendet.'],
    [2, 3, 'Nur Song 3 wird verwendet.'],
    [3, 2, 'Diese Auswahl enthält keine Songs.'],
    [-3, -1, 'Vom drittletzten Song bis vor den letzten Song werden die Songs verwendet.'],
    [1, -2, 'Ab Song 2 werden alle Songs außer den letzten 2 Songs verwendet.'],
    [-2, 3, 'Vom vorletzten Song bis einschließlich Song 3 werden Songs verwendet; bei kurzen Setlists kann die Auswahl leer sein.'],
  ])('beschreibt die Song-Auswahl %s:%s verständlich', (start, end, expected) => {
    expect(describeSongSelection(start, end)).toBe(expected)
  })

  it('beschreibt ungültige Zwischenstände ohne technische Werte', () => {
    expect(describeSongSelection('', null)).toBe('Bitte gib einen ganzzahligen Startwert ein.')
    expect(describeSongSelection(null, null)).toBe('Bitte gib einen ganzzahligen Startwert ein.')
    expect(describeSongSelection(0, 1.5)).toBe('Bitte verwende für das Ende eine ganze Zahl oder lasse es leer.')
  })

  it('verwendet sichere und ressourcenschonende Standardwerte', () => {
    const profile = newProfile()
    expect(profile).toMatchObject({
      enabled: false,
      lookahead_days: 28,
      interval_minutes: 60,
      create_missing_songs: true,
      sync_mode: 'source_changes_only',
      match_mode: 'exact_time',
    })
    expect(profile.agenda_item_defaults).toEqual({
      title: null,
      note: null,
      responsible: null,
      duration: null,
    })
  })

  it('übernimmt das gewählte Sync-Verhalten beim Bearbeiten eines Profils', () => {
    const persisted: SyncProfile = {
      ...newProfile(),
      id: 'profile-1',
      workspace_id: 'workspace-1',
      sync_mode: 'enforce_source',
      next_scheduled_at: null,
      delete_blockers: [],
      revision: 2,
      created_at: '2026-08-23T00:00:00Z',
      updated_at: '2026-08-23T00:00:00Z',
    }

    expect(profileInputFromProfile(persisted).sync_mode).toBe('enforce_source')
  })

  it('erzwingt das Mindestintervall und bereinigt optionale Regeln', () => {
    const profile = newProfile()
    profile.name = '  Sonntags  '
    profile.interval_minutes = 10
    profile.event_rules[0]!.name_regex = '  '

    const result = sanitizeProfile(profile)

    expect(result.name).toBe('Sonntags')
    expect(result.interval_minutes).toBe(30)
    expect(result.event_rules[0]!.name_regex).toBeUndefined()
  })

  it('bereinigt auch Vue-Proxy-Daten aus Editor und Onboarding', () => {
    const profile = reactive(newProfile())
    profile.name = '  Reaktiv  '

    expect(sanitizeProfile(profile).name).toBe('Reaktiv')
  })

  it('bewahrt negative Song-Grenzen für relative Slices', () => {
    const profile = newProfile()
    profile.placements = [
      { ...profile.placements[0]!, song_start: 0, song_end: -1 },
      { ...profile.placements[0]!, id: 'closing', song_start: -1, song_end: null },
    ]

    const result = sanitizeProfile(profile)

    expect(result.placements.map(({ song_start, song_end }) => ({ song_start, song_end }))).toEqual([
      { song_start: 0, song_end: -1 },
      { song_start: -1, song_end: null },
    ])
  })

  it('sendet ausschließlich kanonische Array-Eventfilter', () => {
    const profile = newProfile()
    const legacyRule = profile.event_rules[0]! as typeof profile.event_rules[number] & {
      calendar_id: string
      campus_name: string
    }
    legacyRule.calendar_id = 'hidden-calendar'
    legacyRule.campus_name = 'Hidden campus'
    legacyRule.calendar_ids = [' calendar-1 ', 'calendar-1', '']
    legacyRule.campus_ids = [' campus-1 ']

    const [rule] = sanitizeProfile(profile).event_rules

    expect(rule).toEqual({
      name_contains: undefined,
      name_regex: undefined,
      calendar_ids: ['calendar-1'],
      campus_ids: ['campus-1'],
    })
    expect(rule).not.toHaveProperty('calendar_id')
    expect(rule).not.toHaveProperty('campus_name')
  })

  it('bereinigt Agenda-Standardwerte ohne Typ- oder Feldverlust', () => {
    const profile = newProfile()
    profile.agenda_item_defaults = {
      title: '  Lobpreis  ',
      note: 'Zeile 1\nZeile 2',
      responsible: '  [Worship Leader] ',
      duration: 300,
    }

    const result = sanitizeProfile(profile)

    expect(result.agenda_item_defaults).toEqual({
      title: 'Lobpreis',
      note: 'Zeile 1\nZeile 2',
      responsible: '[Worship Leader]',
      duration: 300,
    })
  })

  it('führt Metadaten-Envelope und Profil-Input ohne Serverfelder typisiert', () => {
    const metadata: ProviderMetadata = {
      data: {
        calendars: [{ id: '1', name: 'Gottesdienst' }],
        campuses: [],
        song_categories: [{ id: '7', name: 'Lobpreis' }],
      },
      retrieved_at: '2026-08-22T10:00:00Z',
    }
    type HasNextScheduledAt = 'next_scheduled_at' extends keyof SyncProfileInput ? true : false
    const hasNextScheduledAt: HasNextScheduledAt = false

    expect(metadata.data.song_categories[0]?.id).toBe('7')
    expect(hasNextScheduledAt).toBe(false)
  })
})
