#!/usr/bin/env bash
# =====================================================================
# 部署/启动脚本：以 mock 模式或真实供应商模式启动 FastAPI 服务
# 用法：
#   bash scripts/deploy.sh              # 前台启动（默认端口 8765）
#   PORT=9000 bash scripts/deploy.sh    # 自定义端口
# 生产环境请先在 .env / 环境变量中配置 LLM_API_KEY 等，勿用 mock。
# =====================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8765}"
export PYTHONPATH=src

echo "[deploy] 知识库目录: $(pwd)/data/knowledge"
echo "[deploy] 启动服务: http://127.0.0.1:${PORT}"
exec python -m uvicorn onboarding_assistant.main:app --host 0.0.0.0 --port "${PORT}"
