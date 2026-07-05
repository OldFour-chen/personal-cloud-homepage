#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/personal-cloud-homepage}"
APP_DIR="${APP_DIR:-/opt/personal-site-api}"
SHARED_DIR="${SHARED_DIR:-${APP_ROOT}/shared}"
BACKUP_DIR="${BACKUP_DIR:-${APP_ROOT}/backups}"
REPORT_DIR="${REPORT_DIR:-${APP_ROOT}/security-reports}"
REQUEST_DIR="${REQUEST_DIR:-${APP_ROOT}/ops-requests}"
STATUS_FILE="${REQUEST_DIR}/latest_restore_status.json"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RESTORE_FILE="${1:-}"
STARTED_AT="$(date '+%Y-%m-%d %H:%M:%S')"
PRE_RESTORE_BACKUP=""

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1 && command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

mkdir -p "${BACKUP_DIR}" "${REQUEST_DIR}" "${SHARED_DIR}" "${REPORT_DIR}"

json_quote() {
  "${PYTHON_BIN}" -c "import json,sys; print(json.dumps(sys.argv[1], ensure_ascii=False))" "$1"
}

write_status() {
  local status="$1"
  local message="$2"
  local exit_code="$3"
  local finished_at
  finished_at="$(date '+%Y-%m-%d %H:%M:%S')"

  {
    echo "{"
    echo "  \"type\": \"restore\","
    echo "  \"status\": $(json_quote "${status}"),"
    echo "  \"message\": $(json_quote "${message}"),"
    echo "  \"filename\": $(json_quote "${RESTORE_FILE}"),"
    echo "  \"pre_restore_backup\": $(json_quote "${PRE_RESTORE_BACKUP}"),"
    echo "  \"started_at\": $(json_quote "${STARTED_AT}"),"
    echo "  \"finished_at\": $(json_quote "${finished_at}"),"
    echo "  \"exit_code\": ${exit_code}"
    echo "}"
  } > "${STATUS_FILE}"
}

fail() {
  local message="$1"
  local exit_code="${2:-1}"
  write_status "failed" "${message}" "${exit_code}"
  echo "${message}" >&2
  exit "${exit_code}"
}

validate_filename() {
  local filename="$1"
  if [[ -z "${filename}" ]]; then
    fail "恢复失败：备份文件名不能为空" 1
  fi
  if [[ "${filename}" == *"/"* || "${filename}" == *"\\"* || "${filename}" == *".."* ]]; then
    fail "恢复失败：备份文件名不合法" 1
  fi
  if [[ ! "${filename}" =~ ^[A-Za-z0-9._-]+\.tar\.gz$ ]]; then
    fail "恢复失败：仅允许恢复 .tar.gz 归档文件" 1
  fi
}

compose_up() {
  if command -v docker-compose >/dev/null 2>&1; then
    sudo docker-compose -f "${APP_DIR}/docker-compose.yml" up -d redis api1 api2
  else
    sudo docker compose -f "${APP_DIR}/docker-compose.yml" up -d redis api1 api2
  fi
}

compose_stop_api() {
  if command -v docker-compose >/dev/null 2>&1; then
    sudo docker-compose -f "${APP_DIR}/docker-compose.yml" stop api1 api2
  else
    sudo docker compose -f "${APP_DIR}/docker-compose.yml" stop api1 api2
  fi
}

validate_filename "${RESTORE_FILE}"

ARCHIVE_PATH="${BACKUP_DIR}/${RESTORE_FILE}"
if [[ ! -f "${ARCHIVE_PATH}" ]]; then
  fail "恢复失败：备份文件不存在" 1
fi

TMP_DIR="$(mktemp -d)"
TMP_LIST="$(mktemp)"

cleanup() {
  rm -rf "${TMP_DIR}"
  rm -f "${TMP_LIST}"
}
trap cleanup EXIT

if [[ -f "${APP_DIR}/site.db" ]]; then
  printf '%s\n' "${APP_DIR#/}/site.db" >> "${TMP_LIST}"
fi
if [[ -f "${SHARED_DIR}/site.db" ]]; then
  printf '%s\n' "${SHARED_DIR#/}/site.db" >> "${TMP_LIST}"
fi
if [[ -d "${REPORT_DIR}" ]]; then
  printf '%s\n' "${REPORT_DIR#/}" >> "${TMP_LIST}"
fi
if [[ ! -s "${TMP_LIST}" ]]; then
  touch "${TMP_DIR}/restore-placeholder.txt"
  printf '%s\n' "${TMP_DIR#/}/restore-placeholder.txt" > "${TMP_LIST}"
fi

PRE_RESTORE_BACKUP="${BACKUP_DIR}/pre-restore-$(date '+%Y%m%d_%H%M%S').tar.gz"
if ! tar -czf "${PRE_RESTORE_BACKUP}" -C / -T "${TMP_LIST}"; then
  fail "恢复失败：创建 pre-restore 保护备份失败" 1
fi

if ! tar -xzf "${ARCHIVE_PATH}" -C "${TMP_DIR}"; then
  fail "恢复失败：备份归档解压失败" 1
fi

RESTORED_SHARED_DB="${TMP_DIR}/opt/personal-cloud-homepage/shared/site.db"
RESTORED_REPORT_DIR="${TMP_DIR}/opt/personal-cloud-homepage/security-reports"

if [[ ! -f "${RESTORED_SHARED_DB}" ]]; then
  fail "恢复失败：备份中未找到 site.db" 1
fi

compose_stop_api
sudo mkdir -p "${SHARED_DIR}" "${APP_DIR}" "${REPORT_DIR}"
sudo cp -f "${RESTORED_SHARED_DB}" "${SHARED_DIR}/site.db"
sudo cp -f "${RESTORED_SHARED_DB}" "${APP_DIR}/site.db"

if [[ -d "${RESTORED_REPORT_DIR}" ]]; then
  if command -v rsync >/dev/null 2>&1; then
    sudo rsync -a "${RESTORED_REPORT_DIR}/" "${REPORT_DIR}/"
  else
    sudo cp -R "${RESTORED_REPORT_DIR}/." "${REPORT_DIR}/"
  fi
fi

compose_up

curl -fsS http://127.0.0.1/api/cloud/status >/dev/null || fail "恢复失败：/api/cloud/status 健康检查失败" 1
curl -fsS http://127.0.0.1/api/cache/status >/dev/null || fail "恢复失败：/api/cache/status 健康检查失败" 1

"${PYTHON_BIN}" - <<'PY' || fail "恢复失败：Redis 连接校验失败" 1
import json
import sys
import urllib.request

data = json.loads(urllib.request.urlopen("http://127.0.0.1/api/cloud/status", timeout=8).read())
if not data.get("runtime_status", {}).get("redis_connected"):
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(1)
PY

write_status "success" "恢复完成" 0
echo "Restore completed: ${ARCHIVE_PATH}"
