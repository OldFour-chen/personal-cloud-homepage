#!/usr/bin/env bash
set -u

OPS_ROOT="${APP_ROOT:-/opt/personal-cloud-homepage}"
OPS_SCRIPT_DIR="${OPS_SCRIPT_DIR:-${OPS_ROOT}/scripts}"
OPS_REQUEST_DIR="${OPS_REQUEST_DIR:-${OPS_ROOT}/ops-requests}"
SECURITY_SCRIPT="${OPS_SCRIPT_DIR}/security_audit.sh"
BACKUP_SCRIPT="${OPS_SCRIPT_DIR}/backup_to_oss.sh"
SECURITY_REQUEST_FILE="${OPS_REQUEST_DIR}/run_security.request"
BACKUP_REQUEST_FILE="${OPS_REQUEST_DIR}/run_backup.request"
STATUS_FILE="${OPS_REQUEST_DIR}/latest_ops_run.json"
LOCK_FILE="${LOCK_FILE:-/tmp/personal-site-ops-runner.lock}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1 && command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

mkdir -p "${OPS_REQUEST_DIR}"

json_quote() {
  "${PYTHON_BIN}" -c "import json,sys; print(json.dumps(sys.argv[1], ensure_ascii=False))" "$1"
}

write_status() {
  local task_type="$1"
  local status="$2"
  local message="$3"
  local time_key="$4"
  local time_value="$5"
  local exit_code="${6:-}"

  {
    echo "{"
    echo "  \"type\": $(json_quote "${task_type}"),"
    echo "  \"status\": $(json_quote "${status}"),"
    echo "  \"message\": $(json_quote "${message}"),"
    echo "  \"${time_key}\": $(json_quote "${time_value}")$( [[ -n "${exit_code}" ]] && printf ',' )"
    if [[ -n "${exit_code}" ]]; then
      echo "  \"exit_code\": ${exit_code}"
    fi
    echo "}"
  } > "${STATUS_FILE}"
}

run_request() {
  local task_type="$1"
  local request_file="$2"
  local script_file="$3"
  local running_message="$4"
  local success_message="$5"
  local missing_message="$6"
  local failed_message="$7"

  if [[ ! -f "${request_file}" ]]; then
    return 0
  fi

  local started_at
  local finished_at
  local exit_code

  started_at="$(date '+%Y-%m-%d %H:%M:%S')"
  write_status "${task_type}" "running" "${running_message}" "started_at" "${started_at}"

  if [[ ! -f "${script_file}" ]]; then
    rm -f "${request_file}"
    finished_at="$(date '+%Y-%m-%d %H:%M:%S')"
    write_status "${task_type}" "failed" "${missing_message}" "finished_at" "${finished_at}" "127"
    return 127
  fi

  if /bin/bash "${script_file}"; then
    exit_code=0
  else
    exit_code=$?
  fi

  rm -f "${request_file}"
  finished_at="$(date '+%Y-%m-%d %H:%M:%S')"

  if [[ ${exit_code} -eq 0 ]]; then
    write_status "${task_type}" "success" "${success_message}" "finished_at" "${finished_at}" "0"
    return 0
  fi

  write_status "${task_type}" "failed" "${failed_message}" "finished_at" "${finished_at}" "${exit_code}"
  return "${exit_code}"
}

has_request=0
if [[ -f "${SECURITY_REQUEST_FILE}" || -f "${BACKUP_REQUEST_FILE}" ]]; then
  has_request=1
fi

if [[ ${has_request} -eq 0 ]]; then
  exit 0
fi

exec 9>"${LOCK_FILE}"
if ! command -v flock >/dev/null 2>&1; then
  echo "flock command not found" >&2
  exit 1
fi
if ! flock -n 9; then
  exit 0
fi

if [[ ! -f "${SECURITY_REQUEST_FILE}" && ! -f "${BACKUP_REQUEST_FILE}" ]]; then
  exit 0
fi

run_request \
  "security_audit" \
  "${SECURITY_REQUEST_FILE}" \
  "${SECURITY_SCRIPT}" \
  "安全巡检正在宿主机执行" \
  "安全巡检执行完成" \
  "安全巡检脚本不存在，请检查服务器脚本同步" \
  "安全巡检执行失败，请查看 /var/log/personal-site-ops-runner.log"
security_exit_code=$?

run_request \
  "backup" \
  "${BACKUP_REQUEST_FILE}" \
  "${BACKUP_SCRIPT}" \
  "备份正在宿主机执行" \
  "备份执行完成" \
  "备份脚本不存在，请检查服务器脚本同步" \
  "备份执行失败，请查看 /var/log/personal-site-ops-runner.log"
backup_exit_code=$?

if [[ ${backup_exit_code} -ne 0 ]]; then
  exit "${backup_exit_code}"
fi

if [[ ${security_exit_code} -ne 0 ]]; then
  exit "${security_exit_code}"
fi

exit 0
