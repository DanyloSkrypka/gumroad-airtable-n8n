#!/usr/bin/env bash
# Local end-to-end check: real n8n (127.0.0.1 only), both workflows imported, FAKE credentials,
# fake SMTP. Proves: import works, nodes exist, webhook -> code -> if -> airtable wiring, error alerts.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
export N8N_USER_FOLDER="$ROOT/.n8n-local"
export N8N_HOST=127.0.0.1 N8N_LISTEN_ADDRESS=127.0.0.1 N8N_PORT="${N8N_PORT:-5690}"
export N8N_PROTOCOL=http N8N_SECURE_COOKIE=false WEBHOOK_URL="http://127.0.0.1:${N8N_PORT:-5690}/"
export N8N_DIAGNOSTICS_ENABLED=false N8N_VERSION_NOTIFICATIONS_ENABLED=false
export N8N_TEMPLATES_ENABLED=false N8N_PERSONALIZATION_ENABLED=false
export N8N_RUNNERS_ENABLED=true N8N_RUNNERS_BROKER_PORT=5691 GENERIC_TIMEZONE=UTC
N8N="$ROOT/.n8n-runtime/node_modules/.bin/n8n"
[ -x "$N8N" ] || { echo "install: cd .n8n-runtime && npm i n8n"; exit 1; }
if [ ! -f "$N8N_USER_FOLDER/.imported" ]; then
  "$N8N" import:credentials --input="$ROOT/tools/e2e_creds.json"
  "$N8N" import:workflow --input="$ROOT/workflow/gumroad-to-airtable.json"
  "$N8N" import:workflow --input="$ROOT/workflow/gumroad-backfill-reconcile.json"
  "$N8N" publish:workflow --id=GumroadAirtbl001
  touch "$N8N_USER_FOLDER/.imported"
fi
exec "$N8N" start
