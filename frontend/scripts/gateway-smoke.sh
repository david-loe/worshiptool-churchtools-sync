#!/bin/sh
set -eu

image_name="${1:?usage: gateway-smoke.sh IMAGE}"
container_name="worship-sync-gateway-smoke-$$"
smoke_dir="$(mktemp -d)"
smoke_port="${GATEWAY_SMOKE_PORT:-18080}"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  rm -r "$smoke_dir"
}
trap cleanup EXIT INT TERM

docker run --detach --name "$container_name" \
  --user 10001:10001 \
  --read-only \
  --tmpfs /tmp:size=16m,mode=1777 \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  --env API_UPSTREAM=127.0.0.1:1 \
  --env XDG_CONFIG_HOME=/tmp/caddy/config \
  --env XDG_DATA_HOME=/tmp/caddy/data \
  --publish "127.0.0.1:${smoke_port}:8080" \
  "$image_name" >/dev/null

ready=false
for _attempt in $(seq 1 40); do
  if curl --fail --silent "http://127.0.0.1:${smoke_port}/healthz" >"$smoke_dir/health" 2>/dev/null; then
    ready=true
    break
  fi
  sleep 0.25
done
[ "$ready" = true ]
[ ! -s "$smoke_dir/health" ]

api_status="$(curl --silent --show-error --output "$smoke_dir/api" --write-out '%{http_code}' "http://127.0.0.1:${smoke_port}/api/v1/auth/me")"
[ "$api_status" = 502 ]
if grep -q '<div id="app"' "$smoke_dir/api"; then
  echo "API request was incorrectly rewritten to the SPA" >&2
  exit 1
fi

curl --fail --silent --show-error "http://127.0.0.1:${smoke_port}/" >"$smoke_dir/index"
grep -q '<div id="app"' "$smoke_dir/index"
