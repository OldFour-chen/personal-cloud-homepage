#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/personal-cloud-homepage}"
SHARED_DIR="${SHARED_DIR:-${APP_ROOT}/shared}"
REPORT_DIR="${REPORT_DIR:-${APP_ROOT}/security-reports}"
SCRIPTS_DIR="${SCRIPTS_DIR:-${APP_ROOT}/scripts}"
BACKUP_DIR="${BACKUP_DIR:-${APP_ROOT}/backups}"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
LATEST_JSON="${BACKUP_DIR}/latest_backup.json"
PYTHON_BIN="${PYTHON_BIN:-python3}"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
ARCHIVE_PATH="${BACKUP_DIR}/site-backup-${TIMESTAMP}.tar.gz"
TMP_LIST="$(mktemp)"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1 && command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

mkdir -p "${BACKUP_DIR}"

cleanup() {
  rm -f "${TMP_LIST}"
}
trap cleanup EXIT

json_quote() {
  "${PYTHON_BIN}" -c "import json,sys; print(json.dumps(sys.argv[1], ensure_ascii=False))" "$1"
}

record_status() {
  local status="$1"
  local oss_status="$2"
  local size_value="$3"
  cat > "${LATEST_JSON}" <<EOF
{
  "time": $(json_quote "$(date '+%Y-%m-%d %H:%M:%S')"),
  "status": $(json_quote "${status}"),
  "archive": $(json_quote "${ARCHIVE_PATH}"),
  "oss_status": $(json_quote "${oss_status}"),
  "size": $(json_quote "${size_value}")
}
EOF
}

declare -a BACKUP_ITEMS=(
  "${SHARED_DIR}/site.db"
  "${SHARED_DIR}/.env"
  "${REPORT_DIR}"
  "${SCRIPTS_DIR}"
  "${NGINX_DIR}"
)

declare -a EXISTING_ITEMS=()
for item in "${BACKUP_ITEMS[@]}"; do
  if [[ -e "${item}" ]]; then
    EXISTING_ITEMS+=("${item}")
    printf '%s\n' "${item#/}" >> "${TMP_LIST}"
  fi
done

if (( ${#EXISTING_ITEMS[@]} == 0 )); then
  record_status "failed" "skipped" "0B"
  echo "No backup sources found under configured paths." >&2
  exit 1
fi

if tar -czf "${ARCHIVE_PATH}" -C / -T "${TMP_LIST}"; then
  size_value="$(du -h "${ARCHIVE_PATH}" | awk '{print $1}')"
else
  record_status "failed" "skipped" "0B"
  echo "Failed to create local backup archive." >&2
  exit 1
fi

oss_status="skipped"
if [[ -n "${OSS_BACKUP_UPLOAD_CMD:-}" ]]; then
  if eval "${OSS_BACKUP_UPLOAD_CMD} \"${ARCHIVE_PATH}\""; then
    oss_status="uploaded"
  else
    oss_status="failed"
  fi
elif [[ -n "${OSSUTIL_BIN:-}" && -x "${OSSUTIL_BIN}" && -n "${OSS_BACKUP_PREFIX:-}" ]]; then
  if "${OSSUTIL_BIN}" cp "${ARCHIVE_PATH}" "${OSS_BACKUP_PREFIX%/}/$(basename "${ARCHIVE_PATH}")"; then
    oss_status="uploaded"
  else
    oss_status="failed"
  fi
fi

record_status "local_created" "${oss_status}" "${size_value}"

echo "Backup archive written: ${ARCHIVE_PATH}"
echo "Latest backup status: ${LATEST_JSON}"
echo "OSS upload status: ${oss_status}"
