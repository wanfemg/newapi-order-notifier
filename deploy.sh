#!/usr/bin/env bash
#######################################
# newapi-order-notifier 一键部署
#
# 用法（new-api 已按官方文档运行在 Linux 服务器上）:
#   cd newapi-order-notifier && bash deploy.sh
#   或在 new-api 仓库根目录: bash newapi-order-notifier/deploy.sh
#
# 脚本自动完成:
#   1. 检查 docker / docker compose
#   2. 定位运行中的 new-api 容器（可用 NEWAPI_CONTAINER=<容器名> 覆盖）
#   3. 读取它的 SQL_DSN / SQLITE_PATH / docker 网络（含 host 网络模式）
#   4. 生成管理界面口令，写出 .env
#   5. 构建镜像（依赖在构建阶段自动下载）并启动容器
#######################################
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()         { log_error "$*"; exit 1; }

PROJECT_NAME="newapi-order-notifier"

resolve_project_dir() {
  local self_dir="" dir=""
  self_dir=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
  for dir in "${NEWAPI_NOTIFIER_DIR:-}" "$self_dir" "$PWD/$PROJECT_NAME" "$PWD"; do
    if [[ -n "$dir" && -f "$dir/Dockerfile" && -d "$dir/app" ]]; then
      echo "$dir"; return 0
    fi
  done
  return 1
}

PROJECT_DIR=$(resolve_project_dir) \
  || die "未找到项目文件。请在 $PROJECT_NAME 目录内执行，或用 NEWAPI_NOTIFIER_DIR 指定路径"
cd "$PROJECT_DIR"
log_info "项目目录：$PROJECT_DIR"

# 版本号用于在部署横幅和界面里确认“到底升没升上去”
VERSION="1.1.3-max"
log_info "当前版本：$VERSION"

# ---------- 1. 环境检查 ----------

command -v docker >/dev/null 2>&1 || die "未检测到 docker，请先安装 Docker"
if docker compose version >/dev/null 2>&1; then
  DOCKER_COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DOCKER_COMPOSE="docker-compose"
else
  die "未检测到 docker compose，请安装含 compose 插件的 Docker"
fi
docker info >/dev/null 2>&1 || die "docker 未运行或当前用户无访问权限（可尝试用 sudo 重跑）"

# ---------- 2. 定位 new-api 容器 ----------

find_newapi_container() {
  if [[ -n "${NEWAPI_CONTAINER:-}" ]]; then
    echo "$NEWAPI_CONTAINER"; return 0
  fi
  local found=""
  # 按容器名匹配：new-api / new-api-master / my-new-api ...
  found=$(docker ps --format '{{.Names}}' | grep -E '(^|[-_])new-api([-_]|$)' | head -n1 || true)
  [[ -n "$found" ]] && { echo "$found"; return 0; }
  # 按 compose 服务标签匹配
  found=$(docker ps --filter 'label=com.docker.compose.service=new-api' --format '{{.Names}}' | head -n1 || true)
  [[ -n "$found" ]] && { echo "$found"; return 0; }
  # 按镜像名匹配，允许 fork 后缀
  found=$(docker ps --format '{{.Names}}\t{{.Image}}' \
    | awk -F'\t' 'tolower($2) ~ /(^|\/)new-api([-_:]|$)/ {print $1; exit}' || true)
  [[ -n "$found" ]] && { echo "$found"; return 0; }
  return 1
}

container_env() {
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null \
    | awk -v want="$2" -F= '$1 == want { sub(/^[^=]*=/, ""); print; exit }'
}

mount_source() {
  docker inspect -f '{{range .Mounts}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}' "$1" 2>/dev/null \
    | awk -F'|' -v dest="$2" '$2 == dest { print $1; exit }'
}

dsn_host() {
  local dsn="$1"
  if [[ "$dsn" == *"@tcp("* ]]; then
    printf '%s' "$dsn" | sed -E 's/.*@tcp\(([^:)]+).*/\1/'
  elif [[ "$dsn" == *"://"* ]]; then
    printf '%s' "$dsn" | sed -E 's#^[a-z+]+://([^@]*@)?([^:/?]+).*#\2#'
  else
    printf '%s' "$dsn" | sed -E 's/(^| )host=([^ ]+).*/\2/'
  fi
}

rewrite_dsn_host() {
  local dsn="$1" newhost="$2"
  if [[ "$dsn" == *"@tcp("* ]]; then
    printf '%s' "$dsn" | sed -E "s#@tcp\([^:)]+#@tcp(${newhost}#"
  elif [[ "$dsn" == *"://"* ]]; then
    printf '%s' "$dsn" | sed -E "s#(://[^@]*@)[^:/?]+#\1${newhost}#"
  else
    printf '%s' "$dsn" | sed -E "s/host=[^ ]+/host=${newhost}/"
  fi
}

log_info "正在检测 new-api 容器..."
CONTAINER=$(find_newapi_container || true)
[[ -n "$CONTAINER" ]] \
  || die "未找到运行中的 new-api 容器。可用 NEWAPI_CONTAINER=<容器名> 指定后重试"
log_success "找到 new-api 容器：$CONTAINER"

# ---------- 3. 网络与数据源检测 ----------

UI_BIND_PORT=8080
OVERLAY="bridge"
NETWORK_MODE=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$CONTAINER" 2>/dev/null || true)
NETWORK_NAME=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' \
  "$CONTAINER" 2>/dev/null | head -n1 || true)
NETWORK_NAME="${NETWORK_NAME%$'\r'}"

DEFAULT_BRIDGE=false
if [[ "$NETWORK_MODE" == "host" ]]; then
  OVERLAY="host"
  UI_BIND_PORT=8765
  log_warn "new-api 使用 host 网络 → 本服务同样使用 host 网络"
elif [[ -z "$NETWORK_NAME" || "$NETWORK_NAME" == "bridge" || "$NETWORK_NAME" == "default" ]]; then
  DEFAULT_BRIDGE=true
  NETWORK_NAME="bridge"
  log_warn "new-api 在默认 bridge 网络（容器名无法互相解析）→ 将改用数据库容器 IP 连接"
else
  log_success "检测到 docker 网络：$NETWORK_NAME"
fi

SQL_DSN=$(container_env "$CONTAINER" SQL_DSN || true)
SQLITE_PATH_IN=$(container_env "$CONTAINER" SQLITE_PATH || true)
NEWAPI_DATA_HOST=""
SQLITE_PATH_MOUNTED=""

if [[ -z "$SQL_DSN" ]]; then
  # 没配 SQL_DSN 说明 new-api 用的是 SQLite
  if [[ -z "$SQLITE_PATH_IN" ]]; then
    DATA_HOST=$(mount_source "$CONTAINER" "/data" || true)
    DATA_HOST="${DATA_HOST%$'\r'}"
    if [[ -n "$DATA_HOST" ]]; then
      db_file=$(find "$DATA_HOST" -maxdepth 1 -name '*.db' 2>/dev/null | head -n1 || true)
      [[ -n "$db_file" ]] && SQLITE_PATH_IN="/data/$(basename "$db_file")"
      NEWAPI_DATA_HOST="$DATA_HOST"
    fi
  fi
  if [[ -n "$SQLITE_PATH_IN" ]]; then
    src_dir=$(mount_source "$CONTAINER" "$(dirname "$SQLITE_PATH_IN")" || true)
    src_dir="${src_dir%$'\r'}"
    [[ -n "$src_dir" ]] && NEWAPI_DATA_HOST="$src_dir"
    SQLITE_PATH_MOUNTED="/newapi-data/$(basename "$SQLITE_PATH_IN")"
  fi
  if [[ -z "$NEWAPI_DATA_HOST" ]]; then
    log_warn "无法定位 SQLite 文件的宿主机目录，请在界面里手动填写 .db 的绝对路径"
  fi
fi

if [[ "$DEFAULT_BRIDGE" == "true" && -n "$SQL_DSN" ]]; then
  DB_HOST=$(dsn_host "$SQL_DSN")
  DB_CONTAINER=$(docker ps --format '{{.Names}}' \
    | grep -Ei 'postgres|mysql|mariadb' | head -n1 || true)
  if [[ -n "$DB_CONTAINER" ]]; then
    DB_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
      "$DB_CONTAINER" 2>/dev/null | head -n1 || true)
    DB_IP="${DB_IP%$'\r'}"
    if [[ -n "$DB_IP" ]]; then
      SQL_DSN=$(rewrite_dsn_host "$SQL_DSN" "$DB_IP")
      log_info "数据库地址 ${DB_HOST} → ${DB_IP}（容器 ${DB_CONTAINER}）"
    fi
  else
    log_warn "未找到数据库容器，请确认 SQL_DSN 中的地址从本容器可达"
  fi
fi

# ---------- 4. 生成口令并写出 .env ----------

# 界面绑定地址：默认只绑本机（127.0.0.1），不对外暴露公网
# 需要从别的机器访问时改成内网地址（如 192.168.1.10）；0.0.0.0 表示对外全开，请自行确保安全组/反代已限制来源
UI_BIND_ADDR=""
if [[ -f .env ]]; then
  UI_BIND_ADDR=$(grep -E '^UI_BIND_ADDR=' .env 2>/dev/null | head -n1 | cut -d'=' -f2- || true)
fi
[[ -n "$UI_BIND_ADDR" ]] || UI_BIND_ADDR="127.0.0.1"

UI_PASSWORD=""
if [[ -f .env ]]; then
  UI_PASSWORD=$(grep -E '^UI_PASSWORD=' .env 2>/dev/null | head -n1 | cut -d'=' -f2- || true)
fi
GENERATED=false
if [[ -z "$UI_PASSWORD" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    UI_PASSWORD=$(openssl rand -base64 24 | tr -d '\n/+=' | head -c 16 || true)
  fi
  if [[ -z "$UI_PASSWORD" ]]; then
    UI_PASSWORD=$(head -c 48 /dev/urandom | base64 | tr -d '\n/+=' | head -c 16)
  fi
  GENERATED=true
fi

{
  printf '# 由 deploy.sh 自动生成，含管理界面口令，请勿提交到公开仓库\n'
  printf 'NEWAPI_CONTAINER=%s\n' "$CONTAINER"
  printf 'NEWAPI_NETWORK_MODE=%s\n' "$OVERLAY"
  printf 'NEWAPI_NETWORK=%s\n' "$NETWORK_NAME"
  printf 'NEWAPI_DATA_HOST=%s\n' "$NEWAPI_DATA_HOST"
  printf 'SQL_DSN=%s\n' "$SQL_DSN"
  printf 'SQLITE_PATH=%s\n' "$SQLITE_PATH_MOUNTED"
  printf 'UI_PASSWORD=%s\n' "$UI_PASSWORD"
  printf 'UI_BIND_PORT=%s\n' "$UI_BIND_PORT"
  printf 'UI_PUBLISH_PORT=8765\n'
  printf 'UI_BIND_ADDR=%s\n' "$UI_BIND_ADDR"
  # host 模式没有端口发布，容器直接监听在宿主机的这个地址上
  if [[ "$OVERLAY" == "host" ]]; then
    printf 'UI_BIND=%s\n' "$UI_BIND_ADDR"
  fi
  printf 'POLL_INTERVAL=10s\n'
  printf 'APP_VERSION=%s\n' "$VERSION"
  printf 'TZ=Asia/Shanghai\n'
} > .env
mkdir -p data
log_success "已写出配置：$PROJECT_DIR/.env"

# ---------- 5. 构建并启动 ----------

export COMPOSE_FILE="docker-compose.yml:docker-compose.${OVERLAY}.yml"
log_info "镜像构建中（所需依赖在此阶段自动下载），首次构建需要几分钟..."
$DOCKER_COMPOSE up -d --build

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
[[ -n "$HOST_IP" ]] || HOST_IP="<服务器IP>"

PORT=$UI_BIND_PORT
if [[ "$OVERLAY" == "bridge" ]]; then
  PORT=8765
fi

# 默认只绑本机，所以给出 SSH 隧道用法：不必为了打开界面而把端口暴露到公网
if [[ "$UI_BIND_ADDR" == "127.0.0.1" || "$UI_BIND_ADDR" == "localhost" ]]; then
  ACCESS_URL="http://127.0.0.1:${PORT}（仅本机）"
  TUNNEL_HINT="ssh -L ${PORT}:127.0.0.1:${PORT} $(whoami)@${HOST_IP}"
elif [[ "$UI_BIND_ADDR" == "0.0.0.0" || "$UI_BIND_ADDR" == "*" ]]; then
  ACCESS_URL="http://${HOST_IP}:${PORT}"
  TUNNEL_HINT=""
else
  ACCESS_URL="http://${UI_BIND_ADDR}:${PORT}"
  TUNNEL_HINT=""
fi

echo ""
echo -e "${BLUE}════════════════════════════════════════════════${NC}"
echo -e "${GREEN} 部署完成${NC}"
echo -e "${BLUE}════════════════════════════════════════════════${NC}"
echo -e "  访问地址 : ${GREEN}${ACCESS_URL}${NC}"
if [[ -n "$TUNNEL_HINT" ]]; then
  echo -e "  本机转发 : 在你自己电脑上执行 ${GREEN}${TUNNEL_HINT}${NC}"
  echo -e "             然后浏览器打开 ${GREEN}http://127.0.0.1:${PORT}${NC}"
fi
echo -e "  绑定地址 : ${UI_BIND_ADDR}（改 .env 的 UI_BIND_ADDR 后重跑本脚本即可换）"
echo -e "  当前版本 : ${GREEN}${VERSION}${NC}"
echo -e "  管理口令 : ${YELLOW}${UI_PASSWORD}${NC}"
if [[ "$GENERATED" == "true" ]]; then
  echo -e "  （口令本次自动生成，已写入 .env，可自行修改后重跑本脚本）"
fi
if [[ "$UI_BIND_ADDR" == "0.0.0.0" || "$UI_BIND_ADDR" == "*" ]]; then
  echo -e "  ${YELLOW}注意：界面已对外绑定 ${UI_BIND_ADDR}，请确认云安全组/防火墙已限制来源，或改用内网地址${NC}"
fi
echo -e "  new-api 容器 : ${CONTAINER}"
echo -e "  数据网络     : ${NETWORK_NAME}（${OVERLAY} 模式）"
if [[ -z "$SQL_DSN" ]]; then
  echo -e "  数据源       : SQLite ${SQLITE_PATH_MOUNTED:-未检测到，请手动指定}"
else
  echo -e "  数据源       : ${SQL_DSN}"
fi
echo -e "  查看日志     : $DOCKER_COMPOSE logs -f order-notifier"
echo ""
echo -e "  打开访问地址后：填写钉钉 Webhook → 点「测试发送」→ 保存配置即可开始推送"
echo -e "${BLUE}════════════════════════════════════════════════${NC}"
