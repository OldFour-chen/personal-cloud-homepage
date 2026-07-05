#!/usr/bin/env bash
set -euo pipefail

OPS_ROOT="${APP_ROOT:-/opt/personal-cloud-homepage}"
OPS_SCRIPT_DIR="${OPS_SCRIPT_DIR:-${OPS_ROOT}/scripts}"
OPS_REQUEST_DIR="${OPS_REQUEST_DIR:-${OPS_ROOT}/ops-requests}"
SECURITY_SCRIPT="${OPS_SCRIPT_DIR}/security_audit.sh"
BACKUP_SCRIPT="${OPS_SCRIPT_DIR}/backup_to_oss.sh"
RESTORE_SCRIPT="${OPS_SCRIPT_DIR}/restore_from_backup.sh"
SELF_HEAL_SCRIPT="${OPS_SCRIPT_DIR}/self_heal.sh"
SECURITY_REQUEST_FILE="${OPS_REQUEST_DIR}/run_security.request"
BACKUP_REQUEST_FILE="${OPS_REQUEST_DIR}/run_backup.request"
RESTORE_REQUEST_FILE="${OPS_REQUEST_DIR}/run_restore.request"
SELF_HEAL_REQUEST_FILE="${OPS_REQUEST_DIR}/run_self_heal.request"
STATUS_FILE="${OPS_REQUEST_DIR}/latest_ops_run.json"
RESTORE_STATUS_FILE="${OPS_REQUEST_DIR}/latest_restore_status.json"
SELF_HEAL_STATUS_FILE="${OPS_REQUEST_DIR}/latest_self_heal_status.json"
LOCK_FILE="${LOCK_FILE:-/tmp/personal-site-ops-runner.lock}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1 && command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

mkdir -p "${OPS_REQUEST_DIR}"

json_quote() {
  "${PYTHON_BIN}" -c "import json,sys; print(json.dumps(sys.argv[1], ensure_ascii=False))" "$1"
}

read_request_field() {
  local request_file="$1"
  local field_name="$2"
  "${PYTHON_BIN}" -c "import json,sys; data=json.load(open(sys.argv[1], encoding='utf-8')); print(data.get(sys.argv[2], ''))" "${request_file}" "${field_name}"
}

write_status() {
  local task_type="$1"
  local status="$2"
  local message="$3"
  local time_key="$4"
  local time_value="$5"
  local extra_json="${6:-{}}"
  local exit_code="${7:-}"

  "${PYTHON_BIN}" - "${STATUS_FILE}" "${task_type}" "${status}" "${message}" "${time_key}" "${time_value}" "${extra_json}" "${exit_code}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "type": sys.argv[2],
    "status": sys.argv[3],
    "message": sys.argv[4],
    sys.argv[5]: sys.argv[6],
}
extra_raw = sys.argv[7].strip()
if extra_raw:
    try:
        extra = json.loads(extra_raw)
        if isinstance(extra, dict):
            payload.update(extra)
    except json.JSONDecodeError:
        pass
exit_code = sys.argv[8].strip()
if exit_code:
    payload["exit_code"] = int(exit_code)
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

promote_status_file() {
  local detail_status_file="$1"
  if [[ -f "${detail_status_file}" ]]; then
    cp -f "${detail_status_file}" "${STATUS_FILE}"
  fi
}

run_request() {
  local task_type="$1"
  local request_file="$2"
  local script_file="$3"
  local running_message="$4"
  local success_message="$5"
  local missing_message="$6"
  local failed_message="$7"
  local detail_status_file="${8:-}"

  if [[ ! -f "${request_file}" ]]; then
    return 0
  fi

  local started_at
  local finished_at
  local exit_code
  local extra_json="{}"
  local filename=""
  local -a command=(/bin/bash "${script_file}")

  if [[ "${task_type}" == "restore" ]]; then
    filename="$(read_request_field "${request_file}" "filename")"
    extra_json="$("${PYTHON_BIN}" -c "import json,sys; print(json.dumps({'filename': sys.argv[1]}, ensure_ascii=False))" "${filename}")"
    command+=( "${filename}" )
  fi

  started_at="$(date '+%Y-%m-%d %H:%M:%S')"
  write_status "${task_type}" "running" "${running_message}" "started_at" "${started_at}" "${extra_json}"

  if [[ ! -f "${script_file}" ]]; then
    rm -f "${request_file}"
    finished_at="$(date '+%Y-%m-%d %H:%M:%S')"
    write_status "${task_type}" "failed" "${missing_message}" "finished_at" "${finished_at}" "${extra_json}" "127"
    return 127
  fi

  if "${command[@]}"; then
    exit_code=0
  else
    exit_code=$?
  fi

  rm -f "${request_file}"
  finished_at="$(date '+%Y-%m-%d %H:%M:%S')"

  if [[ -n "${detail_status_file}" ]]; then
    promote_status_file "${detail_status_file}"
    if [[ -f "${detail_status_file}" ]]; then
      exit_code="$("${PYTHON_BIN}" -c "import json,sys; data=json.load(open(sys.argv[1], encoding='utf-8')); print(int(data.get('exit_code', 0)))" "${detail_status_file}")"
    fi
  fi

  if [[ ${exit_code} -eq 0 ]]; then
    if [[ -z "${detail_status_file}" || ! -f "${detail_status_file}" ]]; then
      write_status "${task_type}" "success" "${success_message}" "finished_at" "${finished_at}" "${extra_json}" "0"
    fi
    return 0
  fi

  if [[ -z "${detail_status_file}" || ! -f "${detail_status_file}" ]]; then
    write_status "${task_type}" "failed" "${failed_message}" "finished_at" "${finished_at}" "${extra_json}" "${exit_code}"
  fi
  return "${exit_code}"
}

has_request=0
for request_file in "${RESTORE_REQUEST_FILE}" "${SELF_HEAL_REQUEST_FILE}" "${SECURITY_REQUEST_FILE}" "${BACKUP_REQUEST_FILE}"; do
  if [[ -f "${request_file}" ]]; then
    has_request=1
    break
  fi
done

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

restore_exit_code=0
self_heal_exit_code=0
security_exit_code=0
backup_exit_code=0

if run_request \
  "restore" \
  "${RESTORE_REQUEST_FILE}" \
  "${RESTORE_SCRIPT}" \
  "恢复任务正在宿主机执行" \
  "恢复任务执行完成" \
  "恢复脚本不存在，请检查服务器脚本同步" \
  "恢复任务执行失败，请查看 /var/log/personal-site-ops-runner.log" \
  "${RESTORE_STATUS_FILE}"; then
  restore_exit_code=0
else
  restore_exit_code=$?
fi

if run_request \
  "self_heal" \
  "${SELF_HEAL_REQUEST_FILE}" \
  "${SELF_HEAL_SCRIPT}" \
  "自愈任务正在宿主机执行" \
  "自愈任务执行完成" \
  "自愈脚本不存在，请检查服务器脚本同步" \
  "自愈任务执行失败，请查看 /var/log/personal-site-ops-runner.log" \
  "${SELF_HEAL_STATUS_FILE}"; then
  self_heal_exit_code=0
else
  self_heal_exit_code=$?
fi

if run_request \
  "security_audit" \
  "${SECURITY_REQUEST_FILE}" \
  "${SECURITY_SCRIPT}" \
  "安全巡检正在宿主机执行" \
  "安全巡检执行完成" \
  "安全巡检脚本不存在，请检查服务器脚本同步" \
  "安全巡检执行失败，请查看 /var/log/personal-site-ops-runner.log"; then
  security_exit_code=0
else
  security_exit_code=$?
fi

if run_request \
  "backup" \
  "${BACKUP_REQUEST_FILE}" \
  "${BACKUP_SCRIPT}" \
  "备份正在宿主机执行" \
  "备份执行完成" \
  "备份脚本不存在，请检查服务器脚本同步" \
  "备份执行失败，请查看 /var/log/personal-site-ops-runner.log"; then
  backup_exit_code=0
else
  backup_exit_code=$?
fi

if [[ ${backup_exit_code} -ne 0 ]]; then
  exit "${backup_exit_code}"
fi
if [[ ${security_exit_code} -ne 0 ]]; then
  exit "${security_exit_code}"
fi
if [[ ${self_heal_exit_code} -ne 0 ]]; then
  exit "${self_heal_exit_code}"
fi
if [[ ${restore_exit_code} -ne 0 ]]; then
  exit "${restore_exit_code}"
fi

exit 0
