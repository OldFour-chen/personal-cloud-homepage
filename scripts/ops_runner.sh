#!/usr/bin/env bash
set -u

OPS_ROOT="${APP_ROOT:-/opt/personal-cloud-homepage}"
OPS_SCRIPT_DIR="${OPS_SCRIPT_DIR:-${OPS_ROOT}/scripts}"
OPS_REQUEST_DIR="${OPS_REQUEST_DIR:-${OPS_ROOT}/ops-requests}"
SECURITY_SCRIPT="${OPS_SCRIPT_DIR}/security_audit.sh"
REQUEST_FILE="${OPS_REQUEST_DIR}/run_security.request"
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
  local status="$1"
  local message="$2"
  local time_key="$3"
  local time_value="$4"
  local exit_code="${5:-}"

  {
    echo "{"
    echo "  \"type\": \"security_audit\","
    echo "  \"status\": $(json_quote "${status}"),"
    echo "  \"message\": $(json_quote "${message}"),"
    echo "  \"${time_key}\": $(json_quote "${time_value}")$( [[ -n "${exit_code}" ]] && printf ',' )"
    if [[ -n "${exit_code}" ]]; then
      echo "  \"exit_code\": ${exit_code}"
    fi
    echo "}"
  } > "${STATUS_FILE}"
}

if [[ ! -f "${REQUEST_FILE}" ]]; then
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

if [[ ! -f "${REQUEST_FILE}" ]]; then
  exit 0
fi

started_at="$(date '+%Y-%m-%d %H:%M:%S')"
write_status "running" "安全巡检正在宿主机执行" "started_at" "${started_at}"

if [[ ! -f "${SECURITY_SCRIPT}" ]]; then
  rm -f "${REQUEST_FILE}"
  finished_at="$(date '+%Y-%m-%d %H:%M:%S')"
  write_status "failed" "安全巡检脚本不存在，请检查服务器脚本同步" "finished_at" "${finished_at}" "127"
  exit 127
fi

/bin/bash "${SECURITY_SCRIPT}"
exit_code=$?
rm -f "${REQUEST_FILE}"
finished_at="$(date '+%Y-%m-%d %H:%M:%S')"

if [[ ${exit_code} -eq 0 ]]; then
  write_status "success" "安全巡检执行完成" "finished_at" "${finished_at}" "0"
  exit 0
fi

write_status "failed" "安全巡检执行失败，请查看 /var/log/personal-site-ops-runner.log" "finished_at" "${finished_at}" "${exit_code}"
exit "${exit_code}"
