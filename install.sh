#!/usr/bin/env bash
#######################################
# newapi-order-notifier 一键部署（引导脚本）
#
# 用法：
#   bash <(curl -sSL https://raw.githubusercontent.com/wanfemg/newapi-order-notifier/main/install.sh)
#
# 作用：
#   1. 检查 curl / git / docker
#   2. 把本体拉取到 ~/newapi-order-notifier（已存在则 git pull 复用）
#   3. 交给 deploy.sh 完成容器探测、配置生成与启动
#
# 可用环境变量覆盖：
#   NOTIFIER_REPO_URL   仓库地址
#   NOTIFIER_BRANCH     分支，默认 main
#   NEWAPI_NOTIFIER_DIR 安装目录，默认 ~/newapi-order-notifier
#######################################
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()         { log_error "$*"; exit 1; }

REPO_URL="${NOTIFIER_REPO_URL:-https://github.com/wanfemg/newapi-order-notifier.git}"
BRANCH="${NOTIFIER_BRANCH:-main}"
TARGET="${NEWAPI_NOTIFIER_DIR:-$HOME/newapi-order-notifier}"

command -v curl >/dev/null 2>&1 || die "未检测到 curl，请先安装 curl"
command -v git  >/dev/null 2>&1 || die "未检测到 git，请先安装 git"
command -v docker >/dev/null 2>&1 || die "未检测到 docker，请先安装 Docker"

if [[ -d "$TARGET/.git" ]]; then
  log_info "目录已存在，更新到最新版本：$TARGET"
  git -C "$TARGET" fetch --depth 1 origin "$BRANCH" >/dev/null 2>&1 || true
  git -C "$TARGET" checkout -q "$BRANCH" 2>/dev/null || true
  if git -C "$TARGET" pull --ff-only origin "$BRANCH" >/dev/null 2>&1; then
    log_info "已是最新版本"
  else
    log_warn "更新失败（可能是本地有改动），沿用本地版本继续部署"
  fi
elif [[ -e "$TARGET" ]]; then
  die "$TARGET 已存在但不是 git 仓库，请手动处理或设置 NEWAPI_NOTIFIER_DIR 指定其它目录"
else
  log_info "拉取项目到：$TARGET"
  git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$TARGET" || die "拉取失败，请检查仓库地址与网络"
fi

cd "$TARGET"
log_info "开始部署..."
exec bash deploy.sh
