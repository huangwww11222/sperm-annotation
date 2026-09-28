#!/usr/bin/env bash
# Linux/macOS entry. Configuration is parsed by Compose, never sourced as shell code.
set -euo pipefail
cd "$(dirname "$0")"
mode="${1:-}"
method="${2:-}"
if [[ "$mode" == "--help" || "$mode" == "-h" ]]; then
  echo '用法：bash deploy.sh [cpu|gpu] [--pull]；首次默认 GPU + AI；已有 .env 时不覆盖配置。'
  exit 0
fi
if [[ -n "$mode" && "$mode" != cpu && "$mode" != gpu ]] || [[ -n "$method" && "$method" != --pull ]]; then
  echo '参数错误。用法：bash deploy.sh [cpu|gpu] [--pull]' >&2; exit 2
fi
command -v docker >/dev/null || { echo '请先安装 Docker Engine/Desktop 和 Compose 插件。' >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo 'Docker 引擎不可用，请启动 Docker 或检查当前用户权限。' >&2; exit 1; }
docker compose version >/dev/null
if [[ ! -f .env ]]; then
  mode="${mode:-gpu}"
  secret=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  files=compose.yaml
  [[ "$mode" != gpu ]] || files=compose.yaml,compose.gpu.yaml
  # Exclusive create protects an existing configuration, including its login secret.
  (umask 077; set -o noclobber; awk -v key="$secret" -v files="$files" '
    /^JWT_SECRET=/ {$0="JWT_SECRET=" key}
    /^COMPOSE_FILE=/ {$0="COMPOSE_FILE=" files}
    {print}
  ' .env.docker.example > .env)
  unset secret
  if [[ "$mode" == gpu ]]; then
    echo "默认启用 SAM3 AI：请准备 NVIDIA GPU、容器 GPU 支持；模型将从本仓库 Release 自动下载并校验，自定义路径在 .env 设置 SAM3_MODEL_HOST_PATH。缺少条件时部署会报错，不自动关闭 AI。"
  fi
  echo "已生成 .env（$mode 模式）；请长期保留，不要提交到仓库。"
elif [[ -n "$mode" ]]; then
  expected=compose.yaml
  [[ "$mode" != gpu ]] || expected=compose.yaml,compose.gpu.yaml
  actual=$(sed -n 's/^COMPOSE_FILE=//p' .env | tr -d '\r')
  if [[ "$actual" != "$expected" ]]; then
    echo "已有 .env 未被覆盖。要切换模式，请设置 COMPOSE_PATH_SEPARATOR=, 和 COMPOSE_FILE=${expected}，再执行脚本。" >&2
    exit 2
  fi
fi
# Explicit project directory keeps bind mounts stable even when invoked elsewhere.
trap 'echo "部署未完成。请查看上方错误；启动失败时运行 docker compose logs --tail=100 backend frontend。数据目录与 .env 均保留。" >&2' ERR
docker compose --project-directory "$PWD" config --quiet
services=$(docker compose --profile model-setup config --services)
if printf '%s\n' "$services" | grep -qx model-setup; then
  docker compose --profile model-setup run --rm --no-deps model-setup
fi
if [[ "$method" == --pull ]]; then
  docker compose pull
  docker compose up -d --no-build --wait --wait-timeout 180
else
  docker compose build --pull
  docker compose up -d --no-build --wait --wait-timeout 180
fi
docker compose ps
echo '部署已就绪。默认访问 http://服务器IP:8080（端口以 .env 中 WEB_PORT 为准）。首次使用请在登录页注册账号。'
