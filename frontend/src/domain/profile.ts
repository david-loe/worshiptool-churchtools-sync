import type { SyncProfile, SyncProfileInput } from '@/api/types'

export function newProfile(): SyncProfileInput {
  return {
    name: 'Standard-Synchronisation',
    enabled: false,
    source_connection_id: '',
    target_connection_id: '',
    match_mode: 'exact_time',
    source_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Berlin',
    target_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Berlin',
    lookahead_days: 28,
    schedule_type: 'interval',
    interval_minutes: 60,
    cron_expression: null,
    event_rules: [{
      name_contains: '',
      name_regex: '',
      calendar_ids: [],
      campus_ids: [],
    }],
    placements: [{
      id: 'main',
      anchor: { item_type: 'header', title: 'Lobpreis' },
      relation: 'after',
      song_start: 0,
      song_end: null,
    }],
    notification_preferences: {
      in_app: true,
      web_push: false,
      email: true,
      telegram: false,
      notify_success: false,
      notify_new_songs: true,
    },
    create_missing_songs: true,
    song_category_id: null,
    arrangement_name: 'Standard-Arrangement',
    agenda_item_defaults: {
      title: null,
      note: null,
      responsible: null,
      duration: null,
    },
  }
}

export function sanitizeProfile(input: SyncProfileInput): SyncProfileInput {
  // Vue refs expose nested objects as Proxies, which structuredClone rejects in
  // browsers. Copy the DTO fields explicitly so editor/onboarding payloads are
  // plain data and response-only fields can never leak into a request.
  const result = cloneProfileInput(input)
  result.name = result.name.trim()
  result.notification_preferences.in_app = true
  result.lookahead_days = Math.min(365, Math.max(1, Number(result.lookahead_days)))
  if (result.schedule_type === 'interval') {
    result.interval_minutes = Math.max(30, Number(result.interval_minutes ?? 60))
    result.cron_expression = null
  } else {
    result.cron_expression = result.cron_expression?.trim() || '0 * * * *'
    result.interval_minutes = null
  }
  result.event_rules = result.event_rules.map((rule) => ({
    name_contains: rule.name_contains?.trim() || undefined,
    name_regex: rule.name_regex?.trim() || undefined,
    calendar_ids: [...new Set(rule.calendar_ids.map((id) => id.trim()).filter(Boolean))],
    campus_ids: [...new Set(rule.campus_ids.map((id) => id.trim()).filter(Boolean))],
  }))
  result.placements = result.placements.map((placement, index) => ({
    ...placement,
    id: placement.id.trim() || `placement-${index + 1}`,
    anchor: {
      item_id: placement.anchor.item_id?.trim() || undefined,
      item_type: placement.anchor.item_type?.trim() || undefined,
      title: placement.anchor.title?.trim() || undefined,
    },
    song_start: Number(placement.song_start),
    song_end: placement.song_end === null || placement.song_end === undefined || String(placement.song_end) === '' ? null : Number(placement.song_end),
  }))
  result.arrangement_name = result.arrangement_name.trim() || 'Standard-Arrangement'
  result.agenda_item_defaults.title = nullableTrimmed(result.agenda_item_defaults.title)
  result.agenda_item_defaults.responsible = nullableTrimmed(result.agenda_item_defaults.responsible)
  if (!result.agenda_item_defaults.note?.trim()) result.agenda_item_defaults.note = null
  const rawDuration = result.agenda_item_defaults.duration
  const duration = Number(rawDuration)
  result.agenda_item_defaults.duration = rawDuration === null || String(rawDuration).trim() === '' || !Number.isFinite(duration)
    ? null
    : Math.min(86_400, Math.max(0, Math.round(duration)))
  return result
}

export function profileInputFromProfile(profile: SyncProfile): SyncProfileInput {
  return cloneProfileInput({
    source_connection_id: profile.source_connection_id,
    target_connection_id: profile.target_connection_id,
    name: profile.name,
    enabled: profile.enabled,
    match_mode: profile.match_mode,
    source_timezone: profile.source_timezone,
    target_timezone: profile.target_timezone,
    lookahead_days: profile.lookahead_days,
    schedule_type: profile.schedule_type,
    interval_minutes: profile.interval_minutes,
    cron_expression: profile.cron_expression,
    event_rules: profile.event_rules,
    placements: profile.placements,
    notification_preferences: profile.notification_preferences,
    create_missing_songs: profile.create_missing_songs,
    song_category_id: profile.song_category_id,
    arrangement_name: profile.arrangement_name,
    agenda_item_defaults: profile.agenda_item_defaults,
  })
}

function cloneProfileInput(input: SyncProfileInput): SyncProfileInput {
  return {
    ...input,
    event_rules: input.event_rules.map((rule) => ({
      ...rule,
      calendar_ids: [...rule.calendar_ids],
      campus_ids: [...rule.campus_ids],
    })),
    placements: input.placements.map((placement) => ({
      ...placement,
      anchor: { ...placement.anchor },
    })),
    notification_preferences: { ...input.notification_preferences },
    agenda_item_defaults: { ...input.agenda_item_defaults },
  }
}

function nullableTrimmed(value: string | null): string | null {
  return value?.trim() || null
}
