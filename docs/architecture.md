# Platform architecture

The platform keeps provider I/O, reconciliation, HTTP delivery and user-facing
state separate. PostgreSQL is the source of truth; Redis is disposable
coordination infrastructure.

```text
Browser/PWA -> Caddy -> FastAPI -> PostgreSQL
                         |  |
                         |  +-> transactional outbox -> notification queue/worker
                         +----> Redis sync/probe queues -> sync workers -> CT / WT
                                  ^
                                  |
                              scheduler
```

## Tenant boundary

Every domain row belongs to a workspace. Membership and role checks are made in
the API and are repeated by PostgreSQL row-level policies. Provider clients,
caches and locks are connection-scoped and are never shared across workspaces.
Secrets are stored separately from display metadata and encrypted with the
configured application encryption key.

Database authorization is split by process:

| Process | PostgreSQL role | Scope |
| --- | --- | --- |
| Regular REST API | `worshipsync_api` | Membership-scoped tenant rows |
| Platform-admin router | `worshipsync_admin` | Workspace quotas/counts and tenant audit inserts only |
| Scheduler and both workers | `worshipsync_worker` | Trusted cross-tenant background processing |
| Migration/bootstrap tools | schema owner | One-shot schema or account administration |

The API never receives worker or owner credentials. Worker RLS access is based
on PostgreSQL `current_user`; caller-settable `app.worker` and
`app.platform_admin` settings are not authorization inputs. The only request
setting consumed by tenant policies is the authenticated user ID. Workspace and
membership policies resolve that ID through owner-executed, argument-limited
predicates so membership RLS cannot recurse; initial ownership and a matching,
unexpired invitation are the only non-member insert paths. Outbox and audit rows
also use RLS: system-wide NULL-workspace rows are generally visible only to the
worker/schema owner, with a narrow exception for the API to enqueue and dedupe
the current account's own e-mail. The admin role cannot create NULL-workspace
audit rows.

## Run lifecycle

1. The scheduler or API creates an immutable run record and enqueues only its ID.
2. A worker acquires a uniquely fenced run lease, continuously renews it while
   fetching provider state and persists a deterministic plan.
3. Ambiguous events are marked without remote writes. Other events can continue.
4. Before each event is applied, the agenda is fetched again and its fingerprint
   checked.
5. Every remote mutation is recorded and verified by reading the target state.
6. The run becomes `succeeded` only if every event was verified, `partial` when
   at least one event is ambiguous or failed, and `failed` when nothing safe was
   applied or infrastructure failed.

Queue delivery is at-least-once. Database uniqueness constraints, per-execution
fencing tokens, remote bindings and post-write reconciliation make actor
execution idempotent. Redis loss can delay work but cannot erase run state.
Scheduled profiles store an indexed `next_scheduled_at`; committed runs whose
broker dispatch was interrupted are redelivered with a bounded backoff.

Notification delivery uses a transactional encrypted outbox. Items are claimed
immediately before delivery, every claim has a unique CAS token, and stale
workers cannot acknowledge or fail a newer attempt. SMTP retries carry a stable
Message-ID. In-app history remains the canonical notification channel.

## Provider boundary

The ChurchTools and WorshipTools adapters return typed provider-neutral DTOs and
raise classified exceptions. Automatic retries are limited to safe reads and
rate-limited responses. A write with an unknown outcome is reconciled against
the provider before any retry.

Only `https://*.church.tools` is accepted for ChurchTools connections. The
undocumented WorshipTools response contract is captured in local contract tests
with sanitised payloads; production credentials and raw browser captures never
enter the repository.
