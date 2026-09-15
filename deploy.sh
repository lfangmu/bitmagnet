#!/usr/bin/env bash
# BitMagnet 一键部署（media 标准版）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "[deploy] 未找到 .env，已从 .env.example 复制" >&2
  cp .env.example .env
fi
set -a; source .env; set +a

DATA_DIR="${DATA_DIR:-./data}"
echo "[deploy] 数据目录: ${DATA_DIR}"
mkdir -p "${DATA_DIR}/postgres" "${DATA_DIR}/config"

echo "[deploy] 构建管理面板并启动全部服务..."
docker compose up -d --build

echo "[deploy] 完成。Web UI: http://localhost:${WEB_PORT:-3333}"
echo "[deploy] 运行验收: ./verify_clean.sh"
