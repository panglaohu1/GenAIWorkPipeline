#!/bin/zsh
# 启动控制台（会自动拉起主力 LLM 服务），浏览器打开 http://127.0.0.1:8701
ROOT="${0:A:h:h}"
cd "$ROOT"
export PYTHONPATH="$ROOT/src"
exec .venv/bin/python -m genai_pipeline.console
