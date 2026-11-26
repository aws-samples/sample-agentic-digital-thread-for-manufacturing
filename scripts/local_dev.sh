#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# local_dev.sh — run the ADT app locally (hybrid stack: local app + Postgres,
# real cloud Bedrock + Neptune). Full runbook: docs/local-development.md
#
# Usage:
#   scripts/local_dev.sh install        # create venv + install the app (editable)
#   scripts/local_dev.sh up             # start + seed local Postgres
#   scripts/local_dev.sh run            # run the app with hot reload (sources .env.local);
#                                       # binds 127.0.0.1 unless ADT_DEV_HOST is set
#   scripts/local_dev.sh down           # stop Postgres (keeps data)
#   scripts/local_dev.sh db-reset       # wipe Postgres volume + re-seed
#   scripts/local_dev.sh psql           # open a psql shell
#   scripts/local_dev.sh logs           # tail Postgres logs
#   scripts/local_dev.sh neptune-create|neptune-status|neptune-destroy [-- args]
#   scripts/local_dev.sh help
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1   # repo root

COMPOSE=(docker compose -f docker-compose.local.yml)
VENV=".venv-local"
PY="$VENV/bin/python"
ENV_FILE=".env.local"

# Compose interpolation requires the database password even for lifecycle
# commands. Load the documented local env file when present, while still
# allowing an explicitly exported value to be used without that file.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "./$ENV_FILE"
  set +a
  # ...and hand the file to compose explicitly. Docker Compose interpolates from
  # the process environment, but nerdctl/podman compose implementations only read
  # the project's env file, so exporting alone leaves POSTGRES_PASSWORD unset and
  # `up` dies with "required variable POSTGRES_PASSWORD is missing a value".
  # Both accept --env-file, and the repo documents .env.local as THE local file.
  COMPOSE+=(--env-file "$ENV_FILE")
fi

usage() {
  cat <<'EOF'
local_dev.sh — run the ADT app locally (hybrid: local app + Postgres, cloud Bedrock + Neptune).
Full runbook: docs/local-development.md

Usage: scripts/local_dev.sh <command>
  install          create venv + install the app (editable)
  up               start + seed local Postgres + Memgraph
  seed             (re)seed ERP/MES view-schemas into local Postgres
  simulate         run erp_simulation cycles to generate transactional data [--cycles N] [--time-step '1 day'] [--scenario NAME]
  sim-loop         run cycles continuously — local stand-in for the deployed rate(1 min) sim Lambda [--interval S] [--scenario NAME]
  dashboard        run the ERP Streamlit dashboard against local Postgres (http://127.0.0.1:8501)
  run              run the app with hot reload (sources .env.local; 127.0.0.1:8000, ADT_DEV_HOST to bind wider)
  down             stop Postgres (keeps data)
  db-reset         wipe Postgres volume + re-seed
  psql             open a psql shell
  logs             tail Postgres logs
  neptune-create   provision a dedicated dev Neptune graph   [-- aws args]
  neptune-status   show the dev Neptune graph status         [-- aws args]
  neptune-destroy  delete the dev Neptune graph              [-- aws args]
  help             show this help
EOF
}

case "${1:-help}" in
  install)
    python3 -m venv "$VENV"
    "$PY" -m pip install --upgrade pip==26.0.1
    "$PY" -m pip install -e "./agentic_digital_thread[local]"
    echo "Installed. Next: cp .env.local.example .env.local && scripts/local_dev.sh up"
    ;;
  up)
    "${COMPOSE[@]}" up -d
    # Wait for Postgres, then seed the ERP/MES view-schemas if they're missing
    # (cmms/plm come from the compose init-dir; erp_data/mes_* come from the seeder).
    for i in $(seq 1 30); do
      docker exec adt-local-postgres pg_isready -U postgres -d adt_demo >/dev/null 2>&1 && break
      sleep 1
    done
    if [[ -x "$VENV/bin/python" && -f "$ENV_FILE" ]]; then
      has=$(docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" adt-local-postgres psql -U postgres -d adt_demo -At \
            -c "SELECT 1 FROM information_schema.schemata WHERE schema_name='erp_data' LIMIT 1" 2>/dev/null)
      if [[ "$has" != "1" ]]; then
        echo "Seeding ERP/MES view-schemas (first run)…"
        ( set -a; . "./$ENV_FILE"; set +a; "$VENV/bin/python" scripts/seed_local_db.py ) \
          || echo "  seed failed — run: scripts/local_dev.sh seed"
      fi
    else
      echo "Tip: run 'install' + create .env.local, then 'scripts/local_dev.sh seed' to enable erp/mes onboarding."
    fi
    echo "Postgres + Memgraph up on localhost (5432 / 7687)."
    ;;
  seed)
    [[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE — run: cp .env.local.example $ENV_FILE" >&2; exit 1; }
    [[ -x "$VENV/bin/python" ]] || { echo "App not installed — run: scripts/local_dev.sh install" >&2; exit 1; }
    set -a; . "./$ENV_FILE"; set +a
    exec "$VENV/bin/python" scripts/seed_local_db.py
    ;;
  simulate)
    [[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE — run: cp .env.local.example $ENV_FILE" >&2; exit 1; }
    [[ -x "$VENV/bin/python" ]] || { echo "App not installed — run: scripts/local_dev.sh install" >&2; exit 1; }
    set -a; . "./$ENV_FILE"; set +a
    shift
    exec "$VENV/bin/python" scripts/simulate_local.py "$@"
    ;;
  sim-loop)
    [[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE — run: cp .env.local.example $ENV_FILE" >&2; exit 1; }
    [[ -x "$VENV/bin/python" ]] || { echo "App not installed — run: scripts/local_dev.sh install" >&2; exit 1; }
    set -a; . "./$ENV_FILE"; set +a
    shift
    # Continuous, real-time cycles (no fast-forward) — the local equivalent of the
    # deployed rate(1 minute) simulation Lambda. Pass --interval/--scenario through.
    exec "$VENV/bin/python" scripts/simulate_local.py --loop --time-step 0 "$@"
    ;;
  dashboard)
    [[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE — run: cp .env.local.example $ENV_FILE" >&2; exit 1; }
    [[ -x "$VENV/bin/python" ]] || { echo "App not installed — run: scripts/local_dev.sh install" >&2; exit 1; }
    set -a; . "./$ENV_FILE"; set +a
    # Streamlit isn't part of the app venv — install the dashboard deps on first use.
    if ! "$VENV/bin/python" -c "import streamlit" >/dev/null 2>&1; then
      echo "Installing ERP dashboard deps (streamlit)…"
      "$VENV/bin/pip" install -q -r erp_simulation/requirements-erp-streamlit.txt
    fi
    # Point the dashboard's DatabaseManager at local Postgres (no Secrets Manager).
    export USE_SECRETS_MANAGER=false CLOUDWATCH_ENABLED=false
    export DB_HOST="${POSTGRES_HOST:-localhost}" DB_PORT="${POSTGRES_PORT:-5432}"
    export DB_NAME="${POSTGRES_DB:-adt_demo}" DB_USER="${POSTGRES_USER:-postgres}"
    : "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in your environment or .env.local}"
    export DB_PASSWORD="$POSTGRES_PASSWORD"
    # The SQL console logs in as its own SELECT-only role, created on first use
    # (erp_simulation/console_login.py); a fresh password per run is enough.
    export CONSOLE_DB_PASSWORD="${CONSOLE_DB_PASSWORD:-$("$VENV/bin/python" -c 'import secrets; print(secrets.token_hex(16))')}"
    export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
    echo "ERP dashboard → http://127.0.0.1:8501  (local Postgres: $DB_NAME)"
    echo "Tip: run 'scripts/local_dev.sh sim-loop' in another shell so scenario buttons produce live data."
    # Bind to localhost only — never expose the dashboard off-box.
    exec "$VENV/bin/streamlit" run erp_simulation/streamlit_dashboard.py \
      --server.port=8501 --server.address=127.0.0.1 \
      --server.headless=true --browser.gatherUsageStats=false
    ;;
  down)
    "${COMPOSE[@]}" down
    ;;
  db-reset)
    "${COMPOSE[@]}" down -v
    exec "$0" up   # re-up + auto re-seed (Postgres init-dir + ERP/MES view-schemas)
    ;;
  logs)
    "${COMPOSE[@]}" logs -f postgres
    ;;
  psql)
    "${COMPOSE[@]}" exec postgres psql -U postgres -d adt_demo
    ;;
  run)
    [[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE — run: cp .env.local.example $ENV_FILE" >&2; exit 1; }
    [[ -x "$VENV/bin/uvicorn" ]] || { echo "App not installed — run: scripts/local_dev.sh install" >&2; exit 1; }
    # The UI is served ONLY from frontend/dist (the legacy static/ tree was
    # removed) — build it once when missing; delete dist/ to force a rebuild.
    if [[ ! -d agentic_digital_thread/frontend/dist ]]; then
      if command -v npm >/dev/null 2>&1; then
        echo "Building frontend/dist (first run)…"
        ( cd agentic_digital_thread/frontend && npm ci --silent && npm run build ) \
          || echo "  frontend build failed — UI unavailable; API still runs. Fix with: cd agentic_digital_thread/frontend && npm ci && npm run build" >&2
      else
        echo "npm not found — UI unavailable (API still runs). Install Node 20+ and run: cd agentic_digital_thread/frontend && npm ci && npm run build" >&2
      fi
    fi
    set -a; . "./$ENV_FILE"; set +a
    cd agentic_digital_thread
    # Loopback by default; set ADT_DEV_HOST (env or .env.local) to bind wider,
    # e.g. ADT_DEV_HOST=0.0.0.0, only on a network you trust.
    exec "../$VENV/bin/uvicorn" agents.server:app --host "${ADT_DEV_HOST:-127.0.0.1}" --port 8000 --reload
    ;;
  neptune-create) shift; exec ./scripts/dev_neptune.sh create "$@" ;;
  neptune-status) shift; exec ./scripts/dev_neptune.sh status "$@" ;;
  neptune-destroy) shift; exec ./scripts/dev_neptune.sh destroy "$@" ;;
  help|-h|--help) usage ;;
  *) echo "Unknown command: $1" >&2; usage; exit 2 ;;
esac
