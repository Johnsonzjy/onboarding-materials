#!/usr/bin/env bash
# =====================================================================
# 构建脚本：固化依赖版本，生成部署用锁文件
# 用法：bash scripts/build.sh
# =====================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

echo "[build] 固化依赖版本 -> requirements.lock.txt"
pip freeze | grep -vE "^(chat|-e )" > requirements.lock.txt

echo "[build] 编译检查全部 Python 模块"
python -m compileall -q src/onboarding_assistant

echo "[build] 运行核心单元测试（mock 模式，无需任何 Key）"
PYTHONPATH=src python -m pytest tests/test_core.py -q

echo "[build] 完成 ✅"
