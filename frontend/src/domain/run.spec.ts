import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { recoverableRunId, runIdFromLocation } from './run'

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
