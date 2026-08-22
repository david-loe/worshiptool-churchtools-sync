export function memberWorkspaceFromQuery(
  value: unknown,
  workspaceIds: readonly string[],
): string | null {
  if (typeof value !== 'string' || !value) return null
  return workspaceIds.includes(value) ? value : null
}
