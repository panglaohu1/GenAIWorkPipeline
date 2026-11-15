#!/bin/zsh
# 本地 FAL 兼容服务（Bonsai 改写编辑指令 + Flux 出图），监听 127.0.0.1:8095。
# 先确保 Bonsai(8081) 与 Flux(8090) 在跑——启动控制台 scripts/start.sh 后它们会按需拉起。
ROOT="${0:A:h:h}"
cd "$ROOT"
export PYTHONPATH="$ROOT/src"
exec .venv/bin/python -m genai_pipeline.fal_shim --port 8095 "$@"
