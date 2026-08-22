# Security model

## Authentication and authorization

- Accounts require a verified e-mail address. Passwords use Argon2id.
- Sessions are opaque and transported only in `Secure`, `HttpOnly`,
  `SameSite=Strict` cookies. Password reset, MFA-sensitive changes and platform
  admin promotion revoke affected sessions.
- State-changing requests require a random CSRF token that is bound to the
  opaque server-side session and repeated in a request header.
- TOTP and single-use recovery codes are supported; TOTP is mandatory for
  platform administrators.
- Platform-admin operations additionally require a recent TOTP-confirmed
  session claim. The default window is 12 hours and can be shortened with
  `WT_SYNC_ADMIN_MFA_MAX_AGE_SECONDS`; replacing or disabling TOTP always
  requires the current password plus a TOTP or consumed recovery code.
- Workspace roles are owner, admin, operator and viewer. Platform administration
  uses a separate permission path and is audited.

## Secrets and logs

- Provider credentials are write-only API values. SMTP, VAPID and optional
  Telegram secrets are injected only into the worker container.
- Provider connection secrets are encrypted with AES-GCM and a versioned master
  key supplied as a runtime secret. The database never contains that master key.
- API responses and persisted errors use classified, redacted error codes;
  upstream bodies, authentication headers, cookies and credentials are not
  intentionally logged.
- Run history, notifications and audit events are removed after 90 days.

Sensitive runtime settings can be supplied as UTF-8 files instead of direct
environment values. The supported file variables are
`WT_SYNC_DATABASE_URL_FILE`, `WT_SYNC_DATABASE_ADMIN_URL_FILE`,
`WT_SYNC_DATABASE_OWNER_URL_FILE`,
`WT_SYNC_REDIS_URL_FILE`, `WT_SYNC_APPLICATION_SECRET_FILE`,
`WT_SYNC_ENCRYPTION_SECRET_FILE`,
`WT_SYNC_ENCRYPTION_PREVIOUS_SECRETS_FILE`,
`WT_SYNC_SMTP_PASSWORD_FILE`, `WT_SYNC_VAPID_PRIVATE_KEY_FILE`,
`WT_SYNC_TELEGRAM_BOT_TOKEN_FILE` and
`WT_SYNC_BOOTSTRAP_ADMIN_PASSWORD_FILE`. For example, use
`WT_SYNC_APPLICATION_SECRET_FILE=/run/secrets/application_secret`.

Never configure a direct value and its `_FILE` counterpart together; startup
fails in that case. Secret files are limited to 64 KiB, reject binary/NUL data
and have exactly one final newline removed (other whitespace is preserved).
File-read failures identify only the setting, never the configured path or
secret contents. Compose mounts only the API DSN plus the restricted admin DSN
into the API, only the worker DSN into scheduler/workers, and only the owner DSN
into migration/bootstrap tools. Raw PostgreSQL passwords are visible only to
PostgreSQL's role-init hook. In production, enabled e-mail verification also
requires a configured SMTP host so registration cannot create accounts that can
never be verified.

API update privileges are column-scoped: tenant routes may rename/archive a
workspace but cannot alter platform-managed quotas, membership identity columns
are immutable, and account flows cannot change activation or platform-admin
flags. Global ownership quotas use an aggregate-only SECURITY-DEFINER function
after locking the target user row, so RLS-hidden workspaces are counted without
exposing their tenant data or admitting concurrent promotions.

PostgreSQL password files contain the exact raw password. The same password in
each owner/API/worker/admin DSN must be URL-percent-encoded when it contains a
reserved URI character; otherwise parsing the DSN can fail independently of
the database role rotation.

Changing a Compose secret file does not alter an existing PostgreSQL role or
invalidate an open connection. The idempotent init hook must be run explicitly
after updating all password and matching DSN files, followed by recreating every
backend service; the exact no-password-in-argv procedure is in the README.

## Network and container boundary

- ChurchTools hosts must use HTTPS and end in `.church.tools`; redirects are
  revalidated before following them.
- Database and Redis have no public Compose ports and live on an internal network.
- The API and gateway communicate through a second internal network. Caddy
  alone is dual-homed onto a separate edge network so Docker can publish its
  loopback-bound host port; the API is never attached to that edge.
- The API has no provider-egress network; only sync and notification workers
  can reach external services.
- Runtime containers use an unprivileged user, a read-only root filesystem,
  dropped Linux capabilities and explicit writable temporary filesystems.
- Frontend and Python dependency graphs are locked; Python artifacts are
  hash-verified. Published images include BuildKit provenance and an SBOM.

Encrypted rows carry a key version. During rotation the runtime can read a
narrowly configured old-key map while writing only with the new key; an offline,
transactional command re-encrypts every retained secret before the old key is
removed.

Production operators must use an external secret manager or Docker secrets,
terminate TLS before the unprivileged Compose gateway (for example at the host
load balancer), keep PostgreSQL backups outside the application host and rotate
all credentials after suspected exposure.
