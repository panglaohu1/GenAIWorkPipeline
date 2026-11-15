#!/bin/bash
# 六边形对偶格 + 瓦片 WFC 地形：用 Blender 渲染参考程序（独立端口，不影响 8701 控制台上正在跑的任务）
cd "$(dirname "$0")/.."
mkdir -p logs
echo "=== $(date) hex trial ===" > logs/oskar_hex_trial.log
GENAI_BLENDER_PORT=9899 PYTHONPATH=src .venv/bin/python scripts/oskar_trial.py \
  projects/oskar-trials/programs/hex-island.ref.json projects/oskar-trials/runs/hex-island >> logs/oskar_hex_trial.log 2>&1
echo "EXIT $?" >> logs/oskar_hex_trial.log
echo "完成，可以关闭这个窗口。"
