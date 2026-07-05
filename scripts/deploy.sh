#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/personal-site-api}"
OPS_ROOT="${OPS_ROOT:-/opt/personal-cloud-homepage}"
COMPOSE_FILE="${COMPOSE_FILE:-${APP_DIR}/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env}"
DB_FILE="${DB_FILE:-${APP_DIR}/site.db}"
BACKUP_DIR="${BACKUP_DIR:-${OPS_ROOT}/deploy-backups}"

mkdir -p "${OPS_ROOT}/scripts" \
         "${OPS_ROOT}/security-reports" \
         "${OPS_ROOT}/backups" \
         "${OPS_ROOT}/ops-requests" \
         "${BACKUP_DIR}"

if [[ ! -d "${APP_DIR}" ]]; then
  echo "ERROR: app directory not found: ${APP_DIR}" >&2
  exit 1
fi

if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(sudo docker-compose)
else
  COMPOSE_CMD=(sudo docker compose)
fi

timestamp="$(date '+%Y%m%d_%H%M%S')"
env_backup=""
db_backup=""

if [[ -f "${ENV_FILE}" ]]; then
  env_backup="${BACKUP_DIR}/.env.${timestamp}.bak"
  cp -f "${ENV_FILE}" "${env_backup}"
fi

if [[ -f "${DB_FILE}" ]]; then
  db_backup="${BACKUP_DIR}/site.db.${timestamp}.bak"
  cp -f "${DB_FILE}" "${db_backup}"
fi

if [[ -d "${APP_DIR}/.git" ]]; then
  git -C "${APP_DIR}" fetch --all --prune
  git -C "${APP_DIR}" pull --ff-only
fi

if [[ -n "${env_backup}" && ! -f "${ENV_FILE}" ]]; then
  cp -f "${env_backup}" "${ENV_FILE}"
fi

if [[ -n "${db_backup}" && ! -f "${DB_FILE}" ]]; then
  cp -f "${db_backup}" "${DB_FILE}"
fi

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "ERROR: docker-compose.yml not found: ${COMPOSE_FILE}" >&2
  exit 1
fi

cd "${APP_DIR}"

"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" config >/dev/null
sudo docker build -t personal-site-api:1.0 "${APP_DIR}"
"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" up -d redis
"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" up -d api1 api2

if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl reload nginx
elif command -v nginx >/dev/null 2>&1; then
  sudo nginx -s reload
fi
