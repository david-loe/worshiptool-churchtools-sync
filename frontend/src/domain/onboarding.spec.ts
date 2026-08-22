import { describe, expect, it } from 'vitest'
import type { Connection, SyncProfile } from '@/api/types'
import { newConnection } from './connection'
import { connectionContinuation, profileContinuation } from './onboarding'
import { newProfile } from './profile'

describe('Onboarding-Fortsetzung', () => {
  it('verwendet eine bereits angelegte Provider-Verbindung gleichen Namens', () => {
    const existing = connection('connection-1', 'churchtools', 'ChurchTools')
    expect(connectionContinuation([existing], newConnection('churchtools'))).toBe(existing)
    expect(connectionContinuation([existing], newConnection('worshiptools'))).toBeUndefined()
  })

  it('setzt nur ein deaktiviertes Profil des gleichen Connection-Paars fort', () => {
    const input = newProfile()
    input.source_connection_id = 'source-1'
    input.target_connection_id = 'target-1'
    const existing = profile('profile-1', false)
    expect(profileContinuation([existing], input)).toBe(existing)
    expect(profileContinuation([{ ...existing, enabled: true }], input)).toBeUndefined()
  })
})

function connection(id: string, provider: Connection['provider'], name: string): Connection {
  return {
    id,
    workspace_id: 'workspace-1',
    provider,
    name,
    settings: {},
    credentials_configured: true,
    revision: 1,
    last_tested_at: null,
    last_test_succeeded: null,
    last_test_message: null,
    delete_blockers: [],
    created_at: '2026-08-22T00:00:00Z',
    updated_at: '2026-08-22T00:00:00Z',
  }
}

function profile(id: string, enabled: boolean): SyncProfile {
  return {
    ...newProfile(),
    id,
    workspace_id: 'workspace-1',
    source_connection_id: 'source-1',
    target_connection_id: 'target-1',
    enabled,
    next_scheduled_at: null,
    revision: 1,
    delete_blockers: [],
    created_at: '2026-08-22T00:00:00Z',
    updated_at: '2026-08-22T00:00:00Z',
  }
}
