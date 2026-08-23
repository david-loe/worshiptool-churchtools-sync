#!/bin/sh
set -eu

# When file-backed secrets are used, read the three application-role passwords
# while the official image still runs as root. Its entrypoint drops to the
# postgres user before executing initdb hooks. The owner password remains
# handled by the official POSTGRES_PASSWORD_FILE implementation.
load_role_password() {
  target_name=$1
  file_name=$2
  secret_path=$3
  direct_value=$4
  if [ -n "$direct_value" ] && [ -n "$secret_path" ]; then
    echo "$target_name and $file_name must not both be set" >&2
    exit 1
  fi
  if [ -z "$secret_path" ]; then
    return
  fi
  if [ ! -r "$secret_path" ]; then
    echo "$file_name could not be read" >&2
    exit 1
  fi
  secret_value=
  exec 3<"$secret_path"
  IFS= read -r secret_value <&3 || [ -n "$secret_value" ]
  extra_line=
  if IFS= read -r extra_line <&3 || [ -n "$extra_line" ]; then
    exec 3<&-
    echo "$file_name must contain exactly one line" >&2
    exit 1
  fi
  exec 3<&-
  if [ -z "$secret_value" ]; then
    echo "$file_name must not be empty" >&2
    exit 1
  fi
  export "$target_name=$secret_value"
  unset "$file_name"
}

load_role_password \
  POSTGRES_API_PASSWORD \
  POSTGRES_API_PASSWORD_FILE \
  "${POSTGRES_API_PASSWORD_FILE:-}" \
  "${POSTGRES_API_PASSWORD:-}"
load_role_password \
  POSTGRES_WORKER_PASSWORD \
  POSTGRES_WORKER_PASSWORD_FILE \
  "${POSTGRES_WORKER_PASSWORD_FILE:-}" \
  "${POSTGRES_WORKER_PASSWORD:-}"
load_role_password \
  POSTGRES_ADMIN_PASSWORD \
  POSTGRES_ADMIN_PASSWORD_FILE \
  "${POSTGRES_ADMIN_PASSWORD_FILE:-}" \
  "${POSTGRES_ADMIN_PASSWORD:-}"
unset secret_value extra_line secret_path direct_value target_name file_name

exec /usr/local/bin/docker-entrypoint.sh "$@"
