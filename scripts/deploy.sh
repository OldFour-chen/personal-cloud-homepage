#!/usr/bin/env bash
set -euo pipefail

OPS_ROOT="${OPS_ROOT:-/opt/personal-cloud-homepage}"
REPO_DIR="${REPO_DIR:-${OPS_ROOT}/repo}"
SHARED_DIR="${SHARED_DIR:-${OPS_ROOT}/shared}"
APP_DIR="${APP_DIR:-/opt/personal-site-api}"
WEB_ROOT="${WEB_ROOT:-/var/www/html}"
BACKUP_DIR="${BACKUP_DIR:-${OPS_ROOT}/deploy-backups}"

REPO_FRONTEND_DIR="${REPO_FRONTEND_DIR:-${REPO_DIR}/frontend}"
REPO_BACKEND_DIR="${REPO_BACKEND_DIR:-${REPO_DIR}/backend}"
REPO_SCRIPTS_DIR="${REPO_SCRIPTS_DIR:-${REPO_DIR}/scripts}"

ENV_FILE="${ENV_FILE:-${APP_DIR}/.env}"
DB_FILE="${DB_FILE:-${APP_DIR}/site.db}"
SHARED_ENV_FILE="${SHARED_ENV_FILE:-${SHARED_DIR}/.env}"
SHARED_DB_FILE="${SHARED_DB_FILE:-${SHARED_DIR}/site.db}"
COMPOSE_FILE="${COMPOSE_FILE:-${APP_DIR}/docker-compose.yml}"

mkdir -p "${OPS_ROOT}/scripts" \
         "${OPS_ROOT}/security-reports" \
         "${OPS_ROOT}/backups" \
         "${OPS_ROOT}/ops-requests" \
         "${BACKUP_DIR}" \
         "${REPO_DIR}" \
         "${SHARED_DIR}"

for required_dir in "${REPO_FRONTEND_DIR}" "${REPO_BACKEND_DIR}" "${REPO_SCRIPTS_DIR}"; do
  if [[ ! -d "${required_dir}" ]]; then
    echo "ERROR: synced repository directory not found: ${required_dir}" >&2
    exit 1
  fi
done

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

if [[ -n "${env_backup}" && ! -f "${SHARED_ENV_FILE}" ]]; then
  cp -f "${env_backup}" "${SHARED_ENV_FILE}"
fi

if [[ -n "${db_backup}" && ! -f "${SHARED_DB_FILE}" ]]; then
  cp -f "${db_backup}" "${SHARED_DB_FILE}"
fi

sync_dir() {
  local src="$1"
  local dst="$2"

  sudo mkdir -p "${dst}"
  if command -v rsync >/dev/null 2>&1; then
    sudo rsync -a --delete "${src}/" "${dst}/"
  else
    echo "WARNING: rsync not found; syncing without delete: ${src} -> ${dst}"
    (
      cd "${src}"
      tar -cf - .
    ) | (
      cd "${dst}"
      sudo tar -xf -
    )
  fi
}

sync_backend_dir() {
  local src="$1"
  local dst="$2"

  sudo mkdir -p "${dst}"
  if command -v rsync >/dev/null 2>&1; then
    sudo rsync -a --delete \
      --exclude '.env' \
      --exclude 'site.db' \
      --exclude 'venv/' \
      --exclude '__pycache__/' \
      "${src}/" "${dst}/"
  else
    echo "WARNING: rsync not found; syncing backend without delete: ${src} -> ${dst}"
    (
      cd "${src}"
      tar --exclude='.env' --exclude='site.db' --exclude='venv' --exclude='__pycache__' -cf - .
    ) | (
      cd "${dst}"
      sudo tar -xf -
    )
  fi
}

install_ops_runner_timer() {
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "WARNING: systemctl not found; skip CloudHome ops runner timer installation"
    return 0
  fi

  local service_file="/etc/systemd/system/cloudhome-ops-runner.service"
  local timer_file="/etc/systemd/system/cloudhome-ops-runner.timer"

  sudo tee "${service_file}" >/dev/null <<'EOF'
[Unit]
Description=CloudHome Ops Runner
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
Group=root
ExecStart=/bin/bash /opt/personal-cloud-homepage/scripts/ops_runner.sh
EOF

  sudo tee "${timer_file}" >/dev/null <<'EOF'
[Unit]
Description=Run CloudHome Ops Runner every 30 seconds

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
AccuracySec=5s
Unit=cloudhome-ops-runner.service

[Install]
WantedBy=timers.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable --now cloudhome-ops-runner.timer
  sudo systemctl restart cloudhome-ops-runner.timer
  sudo systemctl is-enabled cloudhome-ops-runner.timer
  sudo systemctl is-active cloudhome-ops-runner.timer
  sudo systemctl list-timers --all | grep cloudhome-ops-runner || true
}

echo "Skip server-side git pull; code is synced by GitHub Actions"
sync_dir "${REPO_FRONTEND_DIR}" "${WEB_ROOT}"
sync_backend_dir "${REPO_BACKEND_DIR}" "${APP_DIR}"
sync_dir "${REPO_SCRIPTS_DIR}" "${OPS_ROOT}/scripts"
sudo chmod +x "${OPS_ROOT}/scripts/"*.sh

if [[ -f "${SHARED_ENV_FILE}" ]]; then
  sudo cp -f "${SHARED_ENV_FILE}" "${ENV_FILE}"
elif [[ -n "${env_backup}" ]]; then
  cp -f "${env_backup}" "${ENV_FILE}"
fi

if [[ -f "${SHARED_DB_FILE}" ]]; then
  sudo cp -f "${SHARED_DB_FILE}" "${DB_FILE}"
elif [[ -n "${db_backup}" ]]; then
  cp -f "${db_backup}" "${DB_FILE}"
fi

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "ERROR: docker-compose.yml not found: ${COMPOSE_FILE}" >&2
  exit 1
fi

cd "${APP_DIR}"

"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" config >/dev/null
sudo docker build -t personal-site-api:1.0 "${APP_DIR}"
echo "Removing legacy containers from the previous deployment flow"
sudo docker rm -f personal-api-1 personal-api-2 personal-redis || true
"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" up -d redis api1 api2
install_ops_runner_timer

if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl reload nginx
elif command -v nginx >/dev/null 2>&1; then
  sudo nginx -s reload
fi
