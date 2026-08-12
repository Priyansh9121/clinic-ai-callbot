#!/usr/bin/env bash
# =============================================================================
# Project-local development services (macOS / Homebrew, no Docker required).
#
# Starts an isolated PostgreSQL cluster and Redis instance that belong to THIS
# project only. Both listen on non-default ports and store their data under
# .devdata/ (gitignored) so that any PostgreSQL/Redis already installed on the
# machine is left completely untouched.
#
#   ./scripts/dev_services.sh init     create the cluster + role + database
#   ./scripts/dev_services.sh start    start postgres + redis
#   ./scripts/dev_services.sh stop     stop postgres + redis
#   ./scripts/dev_services.sh status   show status
#   ./scripts/dev_services.sh destroy  delete all local dev data
#
# If you have Docker, prefer `docker compose up -d postgres redis` instead;
# docker-compose.yml exposes the same ports and credentials.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVDATA="${REPO_ROOT}/.devdata"
PGDATA="${DEVDATA}/postgres"
PGPORT="${DEV_PGPORT:-5433}"
PGLOG="${DEVDATA}/postgres.log"
REDIS_PORT="${DEV_REDIS_PORT:-6380}"
REDIS_DIR="${DEVDATA}/redis"
REDIS_LOG="${DEVDATA}/redis.log"

DB_NAME="clinic_callbot"
DB_TEST_NAME="clinic_callbot_test"
DB_USER="clinic_app"
DB_PASS="clinic_app"          # development-only credential; never used in production

# --- Locate a *usable* PostgreSQL server installation ------------------------
# We only need the server binaries; the cluster itself lives in .devdata and is
# owned by the current user, so an existing system install is never touched.
#
# A candidate is only accepted if its compiled-in share directory actually
# exists. Homebrew's keg-only postgresql@17 currently reports a sharedir
# (share/postgresql@17) that it does not create, which makes `postgres` fail at
# start-up even though `initdb -L ...` succeeds — so we verify, not assume.
#
# Override with DEV_PG_BIN=/path/to/postgres/bin if auto-detection picks wrong.
find_pg_bin() {
  local candidates=()
  [[ -n "${DEV_PG_BIN:-}" ]] && candidates+=("${DEV_PG_BIN}")
  local brew_prefix
  brew_prefix="$(brew --prefix postgresql@17 2>/dev/null || true)"
  [[ -n "${brew_prefix}" ]] && candidates+=("${brew_prefix}/bin")
  # EDB / postgresql.org installers, newest first
  local d
  for d in $(ls -d /Library/PostgreSQL/*/bin 2>/dev/null | sort -rV); do
    candidates+=("${d}")
  done
  candidates+=("/Applications/Postgres.app/Contents/Versions/latest/bin")

  local bin sharedir
  for bin in "${candidates[@]}"; do
    [[ -x "${bin}/initdb" && -x "${bin}/pg_ctl" && -x "${bin}/pg_config" ]] || continue
    sharedir="$("${bin}/pg_config" --sharedir 2>/dev/null || true)"
    [[ -n "${sharedir}" && -f "${sharedir}/postgres.bki" ]] || continue
    echo "${bin}"
    return 0
  done
  return 1
}

if ! PG_BIN="$(find_pg_bin)"; then
  echo "error: no usable PostgreSQL server installation found." >&2
  echo "       Install one (brew install postgresql@17) or set DEV_PG_BIN." >&2
  exit 1
fi
PG_SHARE="$("${PG_BIN}/pg_config" --sharedir)"

log() { printf '\033[36m==>\033[0m %s\n' "$*"; }

pg_running() { "${PG_BIN}/pg_ctl" -D "${PGDATA}" status >/dev/null 2>&1; }
redis_running() { redis-cli -p "${REDIS_PORT}" ping >/dev/null 2>&1; }

cmd_init() {
  mkdir -p "${DEVDATA}" "${REDIS_DIR}"

  if [[ -f "${PGDATA}/PG_VERSION" ]]; then
    log "postgres cluster already initialised at ${PGDATA}"
  else
    log "initialising postgres cluster at ${PGDATA}"
    # Local-socket/loopback trust auth: acceptable for a throwaway dev cluster
    # that binds to 127.0.0.1 only. Production uses scram-sha-256 (see
    # infrastructure/postgres/).
    "${PG_BIN}/initdb" -D "${PGDATA}" -L "${PG_SHARE}" -U postgres \
      --auth-local=trust --auth-host=trust --encoding=UTF8 --locale=C >/dev/null
    {
      echo "listen_addresses = '127.0.0.1'"
      echo "port = ${PGPORT}"
      echo "unix_socket_directories = '${DEVDATA}'"
      echo "log_min_duration_statement = 500"
    } >> "${PGDATA}/postgresql.conf"
  fi

  cmd_start_pg

  log "creating role ${DB_USER} and database ${DB_NAME} (idempotent)"
  "${PG_BIN}/psql" -h 127.0.0.1 -p "${PGPORT}" -U postgres -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  END IF;
END
\$\$;
SQL
  # The application database and a separate scratch database for the test
  # suite. The app role deliberately lacks CREATEDB (least privilege), so the
  # databases are created here by the cluster superuser instead.
  local db
  for db in "${DB_NAME}" "${DB_TEST_NAME}"; do
    if ! "${PG_BIN}/psql" -h 127.0.0.1 -p "${PGPORT}" -U postgres -lqt \
          | cut -d '|' -f1 | grep -qw "${db}"; then
      "${PG_BIN}/createdb" -h 127.0.0.1 -p "${PGPORT}" -U postgres -O "${DB_USER}" "${db}"
    fi
    "${PG_BIN}/psql" -h 127.0.0.1 -p "${PGPORT}" -U postgres -d "${db}" -v ON_ERROR_STOP=1 \
      -c "REVOKE ALL ON SCHEMA public FROM PUBLIC;" \
      -c "GRANT ALL ON SCHEMA public TO ${DB_USER};" >/dev/null
  done

  log "postgres ready on 127.0.0.1:${PGPORT} (db=${DB_NAME} user=${DB_USER})"
  cmd_start_redis
}

cmd_start_pg() {
  if pg_running; then
    log "postgres already running on port ${PGPORT}"
    return
  fi
  log "starting postgres on port ${PGPORT}"
  "${PG_BIN}/pg_ctl" -D "${PGDATA}" -l "${PGLOG}" -w start >/dev/null
}

cmd_start_redis() {
  if ! command -v redis-server >/dev/null 2>&1; then
    echo "warning: redis-server not installed (brew install redis)." >&2
    echo "         The backend will fall back to the in-process session store." >&2
    return
  fi
  if redis_running; then
    log "redis already running on port ${REDIS_PORT}"
    return
  fi
  log "starting redis on port ${REDIS_PORT}"
  mkdir -p "${REDIS_DIR}"
  redis-server --port "${REDIS_PORT}" --bind 127.0.0.1 --daemonize yes \
    --dir "${REDIS_DIR}" --logfile "${REDIS_LOG}" \
    --save '' --appendonly no          # session state is transient by design
}

cmd_start() { cmd_start_pg; cmd_start_redis; }

cmd_stop() {
  if pg_running; then
    log "stopping postgres"
    "${PG_BIN}/pg_ctl" -D "${PGDATA}" -m fast -w stop >/dev/null
  fi
  if command -v redis-cli >/dev/null 2>&1 && redis_running; then
    log "stopping redis"
    redis-cli -p "${REDIS_PORT}" shutdown nosave >/dev/null 2>&1 || true
  fi
}

cmd_status() {
  pg_running && echo "postgres : running (127.0.0.1:${PGPORT})" || echo "postgres : stopped"
  if command -v redis-cli >/dev/null 2>&1 && redis_running; then
    echo "redis    : running (127.0.0.1:${REDIS_PORT})"
  else
    echo "redis    : stopped/not installed"
  fi
}

cmd_destroy() {
  cmd_stop
  log "removing ${DEVDATA}"
  rm -rf "${DEVDATA}"
}

case "${1:-status}" in
  init)    cmd_init ;;
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  destroy) cmd_destroy ;;
  *) echo "usage: $0 {init|start|stop|status|destroy}" >&2; exit 2 ;;
esac
