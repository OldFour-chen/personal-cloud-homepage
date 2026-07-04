#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/personal-cloud-homepage}"
REPORT_DIR="${REPORT_DIR:-${APP_ROOT}/security-reports}"
BACKUP_DIR="${BACKUP_DIR:-${APP_ROOT}/backups}"
TEXT_REPORT="${REPORT_DIR}/latest_report.txt"
JSON_REPORT="${REPORT_DIR}/latest_report.json"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1 && command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

mkdir -p "${REPORT_DIR}" "${BACKUP_DIR}"

json_quote() {
  "${PYTHON_BIN}" -c "import json,sys; print(json.dumps(sys.argv[1], ensure_ascii=False))" "$1"
}

probe_url() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 8 "${url}" >/dev/null 2>&1
    return $?
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -q -T 8 -O /dev/null "${url}" >/dev/null 2>&1
    return $?
  fi
  return 127
}

add_risk() {
  local level="$1"
  local message="$2"
  if [[ "${level}" == "danger" ]]; then
    DANGER_RISKS+=("${message}")
  else
    WARNING_RISKS+=("${message}")
  fi
}

timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
declare -a DANGER_RISKS=()
declare -a WARNING_RISKS=()
declare -A DOCKER_STATUS=(
  ["personal-api-1"]="missing"
  ["personal-api-2"]="missing"
  ["personal-redis"]="missing"
)

nginx_status="ok"
nginx_output="nginx command not found"
if command -v nginx >/dev/null 2>&1; then
  if nginx_output="$(nginx -t 2>&1)"; then
    nginx_status="ok"
  else
    nginx_status="error"
    add_risk "danger" "Nginx configuration check failed"
  fi
else
  nginx_status="error"
  add_risk "danger" "nginx command not found on server"
fi

docker_ps_output="docker command not found"
if command -v docker >/dev/null 2>&1; then
  docker_ps_output="$(docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' 2>&1 || true)"
  for container_name in "personal-api-1" "personal-api-2" "personal-redis"; do
    running_state="$(docker inspect -f '{{.State.Running}}' "${container_name}" 2>/dev/null || true)"
    if [[ "${running_state}" == "true" ]]; then
      DOCKER_STATUS["${container_name}"]="running"
    elif docker inspect "${container_name}" >/dev/null 2>&1; then
      DOCKER_STATUS["${container_name}"]="stopped"
      add_risk "danger" "Core container stopped: ${container_name}"
    else
      DOCKER_STATUS["${container_name}"]="missing"
      add_risk "danger" "Core container missing: ${container_name}"
    fi
  done
else
  for container_name in "personal-api-1" "personal-api-2" "personal-redis"; do
    add_risk "danger" "docker command not found, cannot inspect container: ${container_name}"
  done
fi

port_snapshot="$(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null || true)"

redis_exposed="no"
if printf '%s\n%s\n' "${docker_ps_output}" "${port_snapshot}" | grep -Eq '0\.0\.0\.0:6379|:::6379'; then
  redis_exposed="yes"
  add_risk "danger" "Redis is exposed on public port 6379"
fi

api_exposed="no"
if printf '%s\n%s\n' "${docker_ps_output}" "${port_snapshot}" | grep -Eq '0\.0\.0\.0:(8001|8002)|:::(8001|8002)'; then
  api_exposed="yes"
  add_risk "danger" "Backend ports 8001/8002 are exposed to the public network"
fi

home_status="ok"
if ! probe_url "http://127.0.0.1/"; then
  home_status="error"
  add_risk "danger" "Home page http://127.0.0.1/ is not reachable"
fi

cloud_api_status="ok"
if ! probe_url "http://127.0.0.1/api/cloud/status"; then
  cloud_api_status="error"
  add_risk "danger" "/api/cloud/status is not reachable"
fi

cache_api_status="ok"
if ! probe_url "http://127.0.0.1/api/cache/status"; then
  cache_api_status="error"
  add_risk "warning" "/api/cache/status is not reachable"
fi

disk_usage="$(df -P / 2>/dev/null | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
disk_usage="${disk_usage:-0}"
if [[ "${disk_usage}" =~ ^[0-9]+$ ]] && (( disk_usage > 80 )); then
  add_risk "warning" "Disk usage is above 80%: ${disk_usage}%"
fi

memory_summary="$(free -h 2>/dev/null | awk 'NR==2 {print "used=" $3 ", free=" $4 ", available=" $7 ", total=" $2}')"
memory_summary="${memory_summary:-unknown}"

failed_ssh_24h="unknown"
if command -v journalctl >/dev/null 2>&1; then
  ssh_log_output="$(journalctl -u ssh --since '24 hours ago' 2>/dev/null || journalctl -u sshd --since '24 hours ago' 2>/dev/null || true)"
  if [[ -n "${ssh_log_output}" ]]; then
    failed_ssh_24h="$(printf '%s' "${ssh_log_output}" | grep -ci 'failed password' || true)"
  else
    failed_ssh_24h="0"
  fi
fi
if [[ "${failed_ssh_24h}" =~ ^[0-9]+$ ]] && (( failed_ssh_24h > 20 )); then
  add_risk "warning" "SSH failed login count in the last 24h is high: ${failed_ssh_24h}"
fi

latest_backup="$(ls -1t "${BACKUP_DIR}"/site-backup-*.tar.gz 2>/dev/null | head -n 1 || true)"
if [[ -z "${latest_backup}" ]]; then
  latest_backup="not_found"
  add_risk "warning" "No recent backup archive was found"
fi

overall_status="ok"
if (( ${#DANGER_RISKS[@]} > 0 )); then
  overall_status="danger"
elif (( ${#WARNING_RISKS[@]} > 0 )); then
  overall_status="warning"
fi

docker_json="{"
first_pair=1
for container_name in "personal-api-1" "personal-api-2" "personal-redis"; do
  if (( first_pair == 0 )); then
    docker_json+=", "
  fi
  docker_json+="$(json_quote "${container_name}")"
  docker_json+=":"
  docker_json+="$(json_quote "${DOCKER_STATUS[${container_name}]}")"
  first_pair=0
done
docker_json+="}"

all_risks=("${DANGER_RISKS[@]}" "${WARNING_RISKS[@]}")
risks_json="["
for idx in "${!all_risks[@]}"; do
  if (( idx > 0 )); then
    risks_json+=", "
  fi
  risks_json+="$(json_quote "${all_risks[${idx}]}")"
done
risks_json+="]"

cat > "${JSON_REPORT}" <<EOF
{
  "time": $(json_quote "${timestamp}"),
  "overall_status": $(json_quote "${overall_status}"),
  "nginx_status": $(json_quote "${nginx_status}"),
  "home_status": $(json_quote "${home_status}"),
  "cloud_api_status": $(json_quote "${cloud_api_status}"),
  "cache_api_status": $(json_quote "${cache_api_status}"),
  "redis_exposed": $(json_quote "${redis_exposed}"),
  "api_exposed": $(json_quote "${api_exposed}"),
  "docker": ${docker_json},
  "disk_usage": ${disk_usage},
  "memory_summary": $(json_quote "${memory_summary}"),
  "failed_ssh_24h": $(json_quote "${failed_ssh_24h}"),
  "latest_backup": $(json_quote "${latest_backup}"),
  "risks": ${risks_json}
}
EOF

{
  echo "Security Audit Report"
  echo "Generated at: ${timestamp}"
  echo "Overall status: ${overall_status}"
  echo
  echo "Core checks"
  echo "- Nginx status: ${nginx_status}"
  echo "- Home page: ${home_status}"
  echo "- Cloud API: ${cloud_api_status}"
  echo "- Cache API: ${cache_api_status}"
  echo "- Redis exposed: ${redis_exposed}"
  echo "- API exposed: ${api_exposed}"
  echo "- Disk usage: ${disk_usage}%"
  echo "- Memory summary: ${memory_summary}"
  echo "- Failed SSH logins (24h): ${failed_ssh_24h}"
  echo "- Latest backup: ${latest_backup}"
  echo
  echo "Docker containers"
  for container_name in "personal-api-1" "personal-api-2" "personal-redis"; do
    echo "- ${container_name}: ${DOCKER_STATUS[${container_name}]}"
  done
  echo
  echo "Risks"
  if (( ${#all_risks[@]} == 0 )); then
    echo "- none"
  else
    for risk in "${all_risks[@]}"; do
      echo "- ${risk}"
    done
  fi
  echo
  echo "Port snapshot"
  printf '%s\n' "${port_snapshot:-no port data}"
  echo
  echo "Docker ps"
  printf '%s\n' "${docker_ps_output}"
  echo
  echo "Nginx -t"
  printf '%s\n' "${nginx_output}"
} > "${TEXT_REPORT}"

echo "Security report written:"
echo "  - ${TEXT_REPORT}"
echo "  - ${JSON_REPORT}"
echo "Overall status: ${overall_status}"
