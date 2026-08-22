import { describe, expect, it } from 'vitest'
import type { Connection } from '@/api/types'
import {
  connectionForEdit,
  connectionPayload,
  connectionUpdatePayload,
  newConnection,
} from './connection'

describe('Verbindungsmodell', () => {
  it('exponiert für WorshipTools keine konfigurierbare Basis-URL', () => {
    const input = newConnection('worshiptools')

    expect(input).not.toHaveProperty('base_url')
    expect(connectionPayload(input, true)).not.toHaveProperty('base_url')
  })

  it('sendet beim Secret-Toggle keinen leeren Credential-Patch', () => {
    const connection = existingWorshipToolsConnection()
    const form = connectionForEdit(connection)

    const payload = connectionPayload(form, true)

    expect(payload.credentials).toBeUndefined()
  })

  it('sendet den unveränderlichen Provider nicht in PATCH-Payloads', () => {
    const form = connectionForEdit(existingWorshipToolsConnection())

    const payload = connectionUpdatePayload(form, false)

    expect(payload).not.toHaveProperty('provider')
    expect(payload).not.toHaveProperty('base_url')
  })

  it('sendet nur tatsächlich eingegebene WorshipTools-Secrets', () => {
    const form = connectionForEdit(existingWorshipToolsConnection())
    if (form.provider !== 'worshiptools') throw new Error('unexpected provider')
    form.credentials = { email: '  ', password: 'new-secret', account_id: '' }

    const payload = connectionPayload(form, true)

    expect(payload.credentials).toEqual({ password: 'new-secret' })
    expect(payload).not.toHaveProperty('base_url')
  })

  it('behält die ChurchTools-Adresse und trimmt das Token', () => {
    const form = newConnection('churchtools')
    if (form.provider !== 'churchtools') throw new Error('unexpected provider')
    form.base_url = 'https://example.church.tools'
    form.credentials = { token: '  Login secret  ' }

    expect(connectionPayload(form, true)).toEqual({
      provider: 'churchtools',
      name: 'ChurchTools',
      base_url: 'https://example.church.tools',
      settings: {},
      credentials: { token: 'Login secret' },
    })
  })
})

function existingWorshipToolsConnection(): Connection {
  return {
    id: 'connection-id',
    workspace_id: 'workspace-id',
    name: 'WorshipTools',
    provider: 'worshiptools',
    base_url: null,
    settings: {},
    credentials_configured: true,
    credential_hint: 'sync@example.org',
    revision: 1,
    last_tested_at: null,
    last_test_succeeded: null,
    last_test_message: null,
    delete_blockers: [],
    created_at: '2026-08-22T00:00:00Z',
    updated_at: '2026-08-22T00:00:00Z',
  }
}
