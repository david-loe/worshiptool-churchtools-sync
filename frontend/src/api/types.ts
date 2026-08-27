export type UUID = string
export type Role = 'owner' | 'admin' | 'operator' | 'viewer'
export type Provider = 'churchtools' | 'worshiptools'
export type RunStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'canceled' | 'skipped'

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface User {
  id: UUID
  email: string
  email_verified_at: string | null
  is_platform_admin: boolean
  totp_enabled: boolean
}

export interface RegisterResponse {
  user: User
  workspace_id: UUID
  verification_required: boolean
  development_verification_token?: string | null
}

export interface SessionResponse {
  user: User
  csrf_token: string
}

export interface VerificationRequestedResponse {
  accepted: boolean
  development_verification_token?: string | null
}

export interface Workspace {
  id: UUID
  name: string
  slug: string
  role: Role
  archived_at: string | null
  profile_quota: number
  member_quota: number
  created_at: string
  updated_at: string
}

export interface WorkspaceMember {
  id: UUID
  user_id?: UUID
  email: string
  display_name?: string | null
  role: Role
  created_at: string
}

export interface WorkspaceInvitation {
  id: UUID
  workspace_id: UUID
  email: string
  role: Role
  created_at: string
  expires_at: string
  accepted_at: string | null
}

export interface ProviderConnectionSettings {
  timezone?: string | null
}

export interface Connection {
  id: UUID
  workspace_id: UUID
  name: string
  provider: Provider
  base_url?: string | null
  settings: ProviderConnectionSettings
  credentials_configured: boolean
  credential_hint?: string | null
  revision: number
  last_tested_at: string | null
  last_test_succeeded: boolean | null
  last_test_message: string | null
  delete_blockers: Array<'profile_reference' | 'remote_binding'>
  created_at: string
  updated_at: string
}

interface ConnectionInputBase {
  name: string
  settings?: ProviderConnectionSettings
}

export interface ChurchToolsConnectionInput extends ConnectionInputBase {
  provider: 'churchtools'
  base_url: string
  credentials?: {
    token?: string
  }
}

export interface WorshipToolsConnectionInput extends ConnectionInputBase {
  provider: 'worshiptools'
  credentials?: {
    email?: string
    password?: string
    account_id?: string
  }
}

export type ConnectionInput = ChurchToolsConnectionInput | WorshipToolsConnectionInput
export type ConnectionUpdateInput =
  | Omit<ChurchToolsConnectionInput, 'provider'>
  | Omit<WorshipToolsConnectionInput, 'provider'>

export interface EventRules {
  name_contains?: string
  name_regex?: string
  calendar_ids: string[]
  campus_ids: string[]
}

export interface AgendaAnchor {
  item_id?: string
  item_type?: string
  title?: string
}

export interface AgendaItemDefaults {
  title: string | null
  note: string | null
  responsible: string | null
  duration: number | null
}

export interface PlacementRules {
  id: string
  anchor: AgendaAnchor
  relation: 'before' | 'at' | 'after'
  multiple_anchor_policy: 'fail' | 'first'
  song_start: number
  song_end: number | null
}

export interface NotificationPreferences {
  in_app: boolean
  web_push: boolean
  email: boolean
  telegram: boolean
  notify_success: boolean
  notify_new_songs: boolean
}

export interface UserNotificationPreferences {
  in_app_enabled: boolean
  push_enabled: boolean
  email_enabled: boolean
  success_notifications: boolean
  telegram_enabled: boolean
}

export interface PushSubscriptionDevice {
  id: UUID
  device_name: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}

export interface SyncProfile {
  id: UUID
  workspace_id: UUID
  name: string
  enabled: boolean
  source_connection_id: UUID
  target_connection_id: UUID
  sync_mode: 'source_changes_only' | 'enforce_source'
  match_mode: 'exact_time' | 'date_only'
  source_timezone: string
  target_timezone: string
  lookahead_days: number
  schedule_type: 'interval' | 'cron'
  interval_minutes: number | null
  cron_expression: string | null
  next_scheduled_at: string | null
  event_rules: EventRules[]
  placements: PlacementRules[]
  notification_preferences: NotificationPreferences
  create_missing_songs: boolean
  song_category_id: number | null
  arrangement_name: string
  agenda_item_defaults: AgendaItemDefaults
  delete_blockers: Array<'run_history' | 'remote_binding'>
  revision: number
  created_at: string
  updated_at: string
}

export type SyncProfileInput = Omit<SyncProfile, 'id' | 'workspace_id' | 'revision' | 'next_scheduled_at' | 'delete_blockers' | 'created_at' | 'updated_at'>

export interface RunStats {
  events_total: number
  events_changed: number
  songs_created: number
  actions_applied: number
  warnings: number
  errors: number
}

export interface SyncAction {
  id: UUID
  event_id?: string
  kind: string
  source_id?: string | null
  target_id?: string | null
  status: 'planned' | 'applied' | 'verified' | 'skipped' | 'failed'
  ordinal: number
  payload: Record<string, unknown>
  fingerprint: Record<string, unknown> | null
  error: Record<string, unknown> | null
  planned_at: string
  applied_at: string | null
  verified_at: string | null
}

export interface SyncRun {
  id: UUID
  workspace_id: UUID
  profile_id: UUID
  profile_name?: string
  status: RunStatus
  trigger: 'scheduled' | 'manual' | 'recovery'
  dry_run: boolean
  created_at: string
  planned_at?: string | null
  started_at?: string | null
  finished_at?: string | null
  plan?: Record<string, unknown> | null
  error: Record<string, unknown> | null
  config_revision: number
}

export interface SyncActionStatusCounts {
  planned: number
  applied: number
  verified: number
  skipped: number
  failed: number
}

export interface SyncActionPage extends Page<SyncAction> {
  status_counts: SyncActionStatusCounts
}

export type RunEventStatus = 'planned' | 'verified' | 'skipped' | 'failed'

export interface RunResultMessage {
  code: string
  message: string
  severity: 'info' | 'warning' | 'error'
  phase: 'plan' | 'execution' | 'run'
  details: Record<string, unknown>
}

export interface CreatedSongResult {
  action_id: string
  source_song_id: string | null
  target_song_id: string | null
  name: string
  author: string
  ccli: string | null
}

export interface RunEventResult {
  id: string
  status: RunEventStatus
  source_event_id: string | null
  target_event_id: string | null
  source_event_name: string | null
  source_event_starts_at: string[] | null
  target_event_name: string | null
  target_event_starts_at: string | null
  messages: RunResultMessage[]
  action_counts: SyncActionStatusCounts
  action_total: number
  new_songs: CreatedSongResult[]
}

export interface SyncRunResult {
  total: number
  planned: number
  verified: number
  skipped: number
  failed: number
  events: RunEventResult[]
  preparation_action_counts: SyncActionStatusCounts
  preparation_action_total: number
}

export interface AppNotification {
  id: UUID
  workspace_id: UUID
  user_id: UUID | null
  severity: 'info' | 'success' | 'warning' | 'error'
  category: string
  title: string
  body: string
  data: Record<string, unknown>
  read_at?: string | null
  created_at: string
  run_id?: UUID | null
  profile_id?: UUID | null
}

export interface NotificationPage extends Page<AppNotification> {
  unread: number
}

export interface NotificationMarkAllReadResponse {
  updated: number
  read_at: string
}

export interface ProblemDetails {
  type?: string
  title: string
  status: number
  detail?: string
  instance?: string
  code?: string
  trace_id?: string
  errors?: Array<{ field: string; message: string; code: string }>
  run_id?: UUID
}

export interface ProviderOption {
  id: string
  name: string
}

export interface ProviderMetadata {
  data: {
    calendars: ProviderOption[]
    campuses: ProviderOption[]
    song_categories: ProviderOption[]
  }
  retrieved_at: string
}

export interface PreviewResult {
  run: SyncRun
  message?: string
}

export interface TotpSetup {
  secret: string
  provisioning_uri: string
  qr_code_svg?: string
}

export interface SystemStatus {
  status: 'ok' | 'degraded'
  version: string
  database: 'ok' | 'error'
  redis: 'ok' | 'error'
}

export interface AdminWorkspace {
  id: UUID
  name: string
  slug: string
  archived_at: string | null
  profile_quota: number
  member_quota: number
  manual_run_cooldown_seconds: 0 | 300 | 900 | 1800
  profile_count: number
  member_count: number
  created_at: string
}
