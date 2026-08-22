# Cutover from the legacy CLI

The legacy YAML cache is not a history database and is intentionally not
imported. Configure the first workspace and profile from scratch.

1. Archive the old CLI's `.env`, `config.yaml` and `db.yaml` outside this
   repository without committing them. The platform uses a fresh `.env` based
   on `.env.example`; do not copy legacy credentials into it wholesale.
2. Create the Compose secret files, start PostgreSQL and migrate the fresh
   schema. On an existing PostgreSQL volume, explicitly run the idempotent role
   hook before revision `0008` as documented in the README. Verify both a
   `pg_dump` and a restore into a separate database before cutover.
3. Start Redis, API, scheduler, worker and gateway. Register and verify the
   workspace owner; bootstrap a separate platform administrator only if needed.
4. Test both provider connections and run a preview. Resolve every unexpected
   match or placement before enabling the schedule.
5. Stop the legacy CLI container. Confirm it is no longer invoked by cron or an
   external scheduler.
6. Enable the new profile and run one manual synchronization.
7. Keep the legacy files only as an offline rollback reference. The repository
   intentionally has no legacy Compose service or YAML cache anymore. Never run
   both implementations with write access at the same time.

Rollback consists of disabling all new profiles and workers before restoring the
old container. Database backups remain necessary even though Redis is disposable.
