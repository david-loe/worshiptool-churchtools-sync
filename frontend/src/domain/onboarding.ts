import type { Connection, ConnectionInput, SyncProfile, SyncProfileInput } from '@/api/types'

export function connectionContinuation(
  connections: readonly Connection[],
  input: ConnectionInput,
): Connection | undefined {
  const name = input.name.trim()
  return connections.find((connection) => connection.provider === input.provider && connection.name === name)
}

export function profileContinuation(
  profiles: readonly SyncProfile[],
  input: SyncProfileInput,
): SyncProfile | undefined {
  const candidates = profiles.filter((profile) => (
    !profile.enabled
    && profile.source_connection_id === input.source_connection_id
    && profile.target_connection_id === input.target_connection_id
  ))
  return candidates.find((profile) => profile.name === input.name.trim())
    ?? (candidates.length === 1 ? candidates[0] : undefined)
}
