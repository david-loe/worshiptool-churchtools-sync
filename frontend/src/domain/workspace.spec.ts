import { describe, expect, it } from 'vitest'
import { memberWorkspaceFromQuery } from './workspace'

describe('memberWorkspaceFromQuery', () => {
  it('accepts only an exact workspace membership', () => {
    expect(memberWorkspaceFromQuery('tenant-b', ['tenant-a', 'tenant-b'])).toBe('tenant-b')
    expect(memberWorkspaceFromQuery('foreign', ['tenant-a', 'tenant-b'])).toBeNull()
    expect(memberWorkspaceFromQuery(['tenant-a'], ['tenant-a'])).toBeNull()
  })
})
