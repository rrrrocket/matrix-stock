#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

mode="${1:-production}"
role="${2:-web}"

case "$mode" in
  production|prod)
    [[ "$role" == "web" ]] || { echo "用法：./start.sh [production|test [web|worker]]" >&2; exit 1; }
    [[ -f .env.production ]] || { echo "缺少 .env.production；先上传生产配置。" >&2; exit 1; }
    env_mode="$(stat -c %a .env.production 2>/dev/null || stat -f %Lp .env.production)"
    [[ "$env_mode" == "600" ]] || { echo "请先执行 chmod 600 .env.production" >&2; exit 1; }
    if grep -q 'replace-with-' .env.production; then
      echo "请先把 .env.production 中的示例密钥替换为真实随机值。" >&2
      exit 1
    fi

    docker compose --env-file .env.production -f compose.production.yml config -q
    docker compose --env-file .env.production -f compose.production.yml up -d --build

    npm_container="${NPM_CONTAINER:-$(docker ps --filter status=running --format '{{.Names}} {{.Image}}' | awk '$2 ~ /^jc21\/nginx-proxy-manager/ { print $1; exit }')}"
    if [[ -n "$npm_container" ]]; then
      if ! docker inspect --format '{{json .NetworkSettings.Networks}}' "$npm_container" | grep -q '"matrix-stock-edge"'; then
        docker network connect matrix-stock-edge "$npm_container"
      fi
      echo "Stock 已启动，NPM 容器 $npm_container 已接入 matrix-stock-edge。"
    else
      echo "Stock 已启动；未自动找到 Nginx Proxy Manager。请执行 NPM_CONTAINER=<容器名> ./start.sh 连接网络。" >&2
    fi
    echo "在 NPM 中将 stock.matrix-one.tech 转发到 http://matrix-stock-web:8020，并申请 HTTPS 证书。"
    ;;
  test|local|worker)
    if [[ "$mode" == "worker" ]]; then role="worker"; fi
    [[ "$role" == "web" || "$role" == "worker" ]] || { echo "用法：./start.sh test [web|worker]" >&2; exit 1; }
    if [[ -f .env ]]; then
      set -a
      source .env
      set +a
    fi
    if [[ ! -x .venv/bin/python ]]; then
      uv venv .venv
    fi
    uv pip install --python .venv/bin/python -r requirements.txt
    .venv/bin/python manage.py migrate
    .venv/bin/python manage.py sync_admin
    if [[ "$role" == "worker" ]]; then
      exec .venv/bin/python manage.py dispatch_alerts
    fi
    exec .venv/bin/python manage.py runserver 127.0.0.1:8020
    ;;
  *)
    echo "用法：./start.sh [production|test [web|worker]]" >&2
    exit 1
    ;;
esac
