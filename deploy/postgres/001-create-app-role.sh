#!/bin/sh
set -eu

# The image bootstrap user owns the schema and runs Alembic. Every long-lived
# service connects with a non-owner role that cannot bypass row-level policies.
read_secret_file() {
  secret_path=$1
  secret_label=$2
  if [ ! -r "$secret_path" ]; then
    echo "$secret_label could not be read" >&2
    exit 1
  fi
  SECRET_VALUE=
  exec 3<"$secret_path"
  IFS= read -r SECRET_VALUE <&3 || [ -n "$SECRET_VALUE" ]
  extra_line=
  if IFS= read -r extra_line <&3 || [ -n "$extra_line" ]; then
    exec 3<&-
    echo "$secret_label must contain exactly one line" >&2
    exit 1
  fi
  exec 3<&-
  if [ -z "$SECRET_VALUE" ]; then
    echo "$secret_label must not be empty" >&2
    exit 1
  fi
}

if [ -n "${POSTGRES_PASSWORD:-}" ] && [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then
  echo "POSTGRES_PASSWORD and POSTGRES_PASSWORD_FILE must not both be set" >&2
  exit 1
fi
if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then
  read_secret_file "$POSTGRES_PASSWORD_FILE" POSTGRES_PASSWORD_FILE
  POSTGRES_PASSWORD=$SECRET_VALUE
fi
if [ -n "${POSTGRES_API_PASSWORD:-}" ] && [ -n "${POSTGRES_API_PASSWORD_FILE:-}" ]; then
  echo "POSTGRES_API_PASSWORD and POSTGRES_API_PASSWORD_FILE must not both be set" >&2
  exit 1
fi
if [ -n "${POSTGRES_API_PASSWORD_FILE:-}" ]; then
  read_secret_file "$POSTGRES_API_PASSWORD_FILE" POSTGRES_API_PASSWORD_FILE
  POSTGRES_API_PASSWORD=$SECRET_VALUE
fi
if [ -n "${POSTGRES_WORKER_PASSWORD:-}" ] && [ -n "${POSTGRES_WORKER_PASSWORD_FILE:-}" ]; then
  echo "POSTGRES_WORKER_PASSWORD and POSTGRES_WORKER_PASSWORD_FILE must not both be set" >&2
  exit 1
fi
if [ -n "${POSTGRES_WORKER_PASSWORD_FILE:-}" ]; then
  read_secret_file "$POSTGRES_WORKER_PASSWORD_FILE" POSTGRES_WORKER_PASSWORD_FILE
  POSTGRES_WORKER_PASSWORD=$SECRET_VALUE
fi
if [ -n "${POSTGRES_ADMIN_PASSWORD:-}" ] && [ -n "${POSTGRES_ADMIN_PASSWORD_FILE:-}" ]; then
  echo "POSTGRES_ADMIN_PASSWORD and POSTGRES_ADMIN_PASSWORD_FILE must not both be set" >&2
  exit 1
fi
if [ -n "${POSTGRES_ADMIN_PASSWORD_FILE:-}" ]; then
  read_secret_file "$POSTGRES_ADMIN_PASSWORD_FILE" POSTGRES_ADMIN_PASSWORD_FILE
  POSTGRES_ADMIN_PASSWORD=$SECRET_VALUE
fi

if [ -z "${POSTGRES_PASSWORD:-}" ]; then
  echo "POSTGRES_PASSWORD or POSTGRES_PASSWORD_FILE must be set" >&2
  exit 1
fi
if [ -z "${POSTGRES_API_PASSWORD:-}" ]; then
  echo "POSTGRES_API_PASSWORD must be set" >&2
  exit 1
fi
if [ -z "${POSTGRES_WORKER_PASSWORD:-}" ]; then
  echo "POSTGRES_WORKER_PASSWORD must be set" >&2
  exit 1
fi
if [ -z "${POSTGRES_ADMIN_PASSWORD:-}" ]; then
  echo "POSTGRES_ADMIN_PASSWORD must be set" >&2
  exit 1
fi

export POSTGRES_PASSWORD POSTGRES_API_PASSWORD POSTGRES_WORKER_PASSWORD POSTGRES_ADMIN_PASSWORD

psql \
  --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<-'SQL'
\getenv owner_role POSTGRES_USER
\getenv owner_password POSTGRES_PASSWORD
\getenv api_password POSTGRES_API_PASSWORD
\getenv worker_password POSTGRES_WORKER_PASSWORD
\getenv admin_password POSTGRES_ADMIN_PASSWORD
\getenv db_name POSTGRES_DB

SELECT format(
  'ALTER ROLE %I WITH PASSWORD %L',
  :'owner_role',
  :'owner_password'
)
\gexec

SELECT format(
  'CREATE ROLE worshipsync_api LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
  :'api_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'worshipsync_api')
\gexec

SELECT format(
  'ALTER ROLE worshipsync_api WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
  :'api_password'
)
\gexec

SELECT format(
  'CREATE ROLE worshipsync_worker LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
  :'worker_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'worshipsync_worker')
\gexec

SELECT format(
  'ALTER ROLE worshipsync_worker WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
  :'worker_password'
)
\gexec

SELECT format(
  'CREATE ROLE worshipsync_admin LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
  :'admin_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'worshipsync_admin')
\gexec

SELECT format(
  'ALTER ROLE worshipsync_admin WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
  :'admin_password'
)
\gexec

GRANT CONNECT ON DATABASE :"db_name"
  TO worshipsync_api, worshipsync_worker, worshipsync_admin;
SQL
