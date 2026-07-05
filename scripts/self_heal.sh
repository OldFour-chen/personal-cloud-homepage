#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/personal-cloud-homepage}"
APP_DIR="${APP_DIR:-/opt/personal-site-api}"
REQUEST_DIR="${REQUEST_DIR:-${APP_ROOT}/ops-requests}"
REPORT_DIR="${REPORT_DIR:-${APP_ROOT}/security-reports}"
STATUS_FILE="${REQUEST_DIR}/latest_self_heal_status.json"
PYTHON_BIN="${PYTHON_BIN:-python3}"
STARTED_AT="$(date '+%Y-%m-%d %H:%M:%S')"
declare -a ACTIONS=()

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1 && command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

mkdir -p "${REQUEST_DIR}"

json_quote() {
  "${PYTHON_BIN}" -c "import json,sys; print(json.dumps(sys.argv[1], ensure_ascii=False))" "$1"
}

write_status() {
  local status="$1"
  local message="$2"
  local exit_code="$3"
  local finished_at
  local actions_json
  finished_at="$(date '+%Y-%m-%d %H:%M:%S')"
  actions_json="$("${PYTHON_BIN}" -c 'import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' "${ACTIONS[@]}")"

  {
    echo "{"
    echo "  \"type\": \"self_heal\","
    echo "  \"status\": $(json_quote "${status}"),"
    echo "  \"message\": $(json_quote "${message}"),"
    echo "  \"actions\": ${actions_json},"
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

compose_up() {
  if command -v docker-compose >/dev/null 2>&1; then
    sudo docker-compose -f "${APP_DIR}/docker-compose.yml" up -d redis api1 api2
  else
    sudo docker compose -f "${APP_DIR}/docker-compose.yml" up -d redis api1 api2
  fi
}

check_service() {
  local service_name="$1"
  if ! command -v systemctl >/dev/null 2>&1; then
    ACTIONS+=("skipped systemctl check for ${service_name}")
    return 0
  fi
  if systemctl is-active --quiet "${service_name}"; then
    ACTIONS+=("checked ${service_name}")
    return 0
  fi
  sudo systemctl restart "${service_name}" || fail "自愈失败：${service_name} 重启失败" 1
  ACTIONS+=("restarted ${service_name}")
}

check_service "nginx"
if command -v nginx >/dev/null 2>&1; then
  sudo nginx -t || fail "自愈失败：nginx 配置校验失败" 1
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl reload nginx || fail "自愈失败：nginx reload 失败" 1
  else
    sudo nginx -s reload || fail "自愈失败：nginx reload 失败" 1
  fi
  ACTIONS+=("validated nginx config")
fi

check_service "docker"

need_restart=0
for container in personal-redis personal-api-1 personal-api-2; do
  if ! sudo docker inspect -f '{{.State.Running}}' "${container}" >/tmp/personal-site-self-heal.inspect 2>/dev/null; then
    need_restart=1
    ACTIONS+=("missing ${container}")
    continue
  fi
  container_running="$(tr -d '\r\n' < /tmp/personal-site-self-heal.inspect)"
  if [[ "${container_running}" != "true" ]]; then
    need_restart=1
    ACTIONS+=("detected stopped ${container}")
  else
    ACTIONS+=("checked ${container}")
  fi
done
rm -f /tmp/personal-site-self-heal.inspect

if [[ ${need_restart} -eq 1 ]]; then
  (
    cd "${APP_DIR}"
    sudo docker rm -f personal-api-1 personal-api-2 personal-redis || true
    compose_up
  ) || fail "自愈失败：容器编排重启失败" 1
  ACTIONS+=("restarted compose stack")
fi

curl -fsS http://127.0.0.1:8001/api/cloud/status >/dev/null || fail "自愈失败：api1 健康检查失败" 1
curl -fsS http://127.0.0.1:8002/api/cloud/status >/dev/null || fail "自愈失败：api2 健康检查失败" 1
curl -fsS http://127.0.0.1/api/cloud/status >/dev/null || fail "自愈失败：主入口云状态检查失败" 1
curl -fsS http://127.0.0.1/api/cache/status >/dev/null || fail "自愈失败：缓存状态检查失败" 1
ACTIONS+=("verified api health endpoints")

"${PYTHON_BIN}" - <<'PY' || fail "自愈失败：Redis 连接状态仍异常" 1
import json
import sys
import urllib.request

data = json.loads(urllib.request.urlopen("http://127.0.0.1/api/cloud/status", timeout=8).read())
if not data.get("runtime_status", {}).get("redis_connected"):
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(1)
PY
ACTIONS+=("verified redis_connected")

sudo docker exec personal-api-1 ls -l /opt/personal-cloud-homepage/security-reports/latest_report.json >/dev/null \
  || fail "自愈失败：api1 未挂载巡检报告 volume" 1
sudo docker exec personal-api-2 ls -l /opt/personal-cloud-homepage/security-reports/latest_report.json >/dev/null \
  || fail "自愈失败：api2 未挂载巡检报告 volume" 1
ACTIONS+=("verified report volume")

write_status "success" "自愈完成，服务已恢复正常" 0
echo "Self-heal completed"
