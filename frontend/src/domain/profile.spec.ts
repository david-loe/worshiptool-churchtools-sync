import { describe, expect, it } from 'vitest'
import { reactive } from 'vue'
import type { ProviderMetadata, SyncProfileInput } from '@/api/types'
import { newProfile, sanitizeProfile } from './profile'

describe('Profilmodell', () => {
  it('verwendet sichere und ressourcenschonende Standardwerte', () => {
    const profile = newProfile()
    expect(profile).toMatchObject({
      enabled: false,
      lookahead_days: 28,
      interval_minutes: 60,
      create_missing_songs: true,
      match_mode: 'exact_time',
    })
    expect(profile.notification_preferences.notify_success).toBe(false)
    expect(profile.notification_preferences.telegram).toBe(false)
    expect(profile.agenda_item_defaults).toEqual({
      title: null,
      note: null,
      responsible: null,
      duration: null,
    })
  })

  it('erzwingt das Mindestintervall und bereinigt optionale Regeln', () => {
    const profile = newProfile()
    profile.name = '  Sonntags  '
    profile.interval_minutes = 10
    profile.notification_preferences.in_app = false
    profile.event_rules[0]!.name_regex = '  '

    const result = sanitizeProfile(profile)

    expect(result.name).toBe('Sonntags')
    expect(result.interval_minutes).toBe(30)
    expect(result.notification_preferences.in_app).toBe(true)
    expect(result.event_rules[0]!.name_regex).toBeUndefined()
  })

  it('bereinigt auch Vue-Proxy-Daten aus Editor und Onboarding', () => {
    const profile = reactive(newProfile())
    profile.name = '  Reaktiv  '

    expect(sanitizeProfile(profile).name).toBe('Reaktiv')
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
