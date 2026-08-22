interface CacheEntry<T> {
  value: T
  expiresAt: number
}

/**
 * Ephemeral GET cache whose entire content is discarded on every tenant switch.
 * Nothing is persisted to localStorage/IndexedDB or the service worker.
 */
export class TenantCache {
  private workspaceId: string | null = null
  private generation = 0
  private readonly entries = new Map<string, CacheEntry<unknown>>()

  activate(workspaceId: string | null): void {
    if (workspaceId === this.workspaceId) return
    this.workspaceId = workspaceId
    this.generation += 1
    this.entries.clear()
  }

  clear(): void {
    this.generation += 1
    this.entries.clear()
  }

  currentWorkspace(): string | null {
    return this.workspaceId
  }

  get<T>(workspaceId: string, key: string): T | undefined {
    if (!this.isActive(workspaceId)) return undefined
    const entry = this.entries.get(key)
    if (!entry) return undefined
    if (entry.expiresAt <= Date.now()) {
      this.entries.delete(key)
      return undefined
    }
    return entry.value as T
  }

  set<T>(workspaceId: string, key: string, value: T, ttlMs: number): void {
    if (!this.isActive(workspaceId)) return
    this.entries.set(key, { value, expiresAt: Date.now() + ttlMs })
  }

  token(workspaceId: string): number {
    if (!this.isActive(workspaceId)) throw new Error('Inaktiver Workspace')
    return this.generation
  }

  isTokenCurrent(workspaceId: string, token: number): boolean {
    return this.isActive(workspaceId) && token === this.generation
  }

  private isActive(workspaceId: string): boolean {
    return this.workspaceId === workspaceId
  }
}

export const tenantCache = new TenantCache()
