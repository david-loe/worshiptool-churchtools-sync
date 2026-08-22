import type {
  ChurchToolsConnectionInput,
  Connection,
  ConnectionInput,
  ConnectionUpdateInput,
  Provider,
  WorshipToolsConnectionInput,
} from '@/api/types'

export function newConnection(provider: Provider): ConnectionInput {
  return provider === 'churchtools'
    ? {
        provider,
        name: 'ChurchTools',
        base_url: '',
        settings: {},
        credentials: { token: '' },
      }
    : {
        provider,
        name: 'WorshipTools',
        settings: {},
        credentials: { email: '', password: '', account_id: '' },
      }
}

export function connectionForEdit(connection: Connection): ConnectionInput {
  return connection.provider === 'churchtools'
    ? {
        provider: 'churchtools',
        name: connection.name,
        base_url: connection.base_url ?? '',
        settings: { ...connection.settings },
        credentials: { token: '' },
      }
    : {
        provider: 'worshiptools',
        name: connection.name,
        settings: { ...connection.settings },
        credentials: { email: '', password: '', account_id: '' },
      }
}

export function resetCredentialFields(input: ConnectionInput): ConnectionInput {
  return input.provider === 'churchtools'
    ? { ...input, credentials: { token: '' } }
    : { ...input, credentials: { email: '', password: '', account_id: '' } }
}

export function connectionPayload(
  input: ConnectionInput,
  includeCredentials: boolean,
): ConnectionInput {
  if (input.provider === 'churchtools') {
    const token = nonBlank(input.credentials?.token, true)
    const result: ChurchToolsConnectionInput = {
      provider: 'churchtools',
      name: input.name,
      base_url: input.base_url,
      settings: { ...(input.settings ?? {}) },
    }
    if (includeCredentials && token !== undefined) result.credentials = { token }
    return result
  }

  const email = nonBlank(input.credentials?.email, true)
  const password = nonBlank(input.credentials?.password, false)
  const accountId = nonBlank(input.credentials?.account_id, true)
  const credentials: NonNullable<WorshipToolsConnectionInput['credentials']> = {}
  if (email !== undefined) credentials.email = email
  if (password !== undefined) credentials.password = password
  if (accountId !== undefined) credentials.account_id = accountId
  const result: WorshipToolsConnectionInput = {
    provider: 'worshiptools',
    name: input.name,
    settings: { ...(input.settings ?? {}) },
  }
  if (includeCredentials && Object.keys(credentials).length) {
    result.credentials = credentials
  }
  return result
}

export function connectionUpdatePayload(
  input: ConnectionInput,
  includeCredentials: boolean,
): ConnectionUpdateInput {
  const { provider: _provider, ...payload } = connectionPayload(input, includeCredentials)
  return payload
}

function nonBlank(value: string | undefined, trim: boolean): string | undefined {
  if (value === undefined || !value.trim()) return undefined
  return trim ? value.trim() : value
}
