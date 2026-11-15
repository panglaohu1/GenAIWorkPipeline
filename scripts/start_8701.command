#!/bin/zsh
# 双击：在新端口 8701 启动 GenAIWorkPipeline 控制台（不结束其它项目占用的 8700 端口进程）。
ROOT="${0:A:h:h}"
echo "启动 GenAIWorkPipeline 控制台（端口 8701）…浏览器打开 http://127.0.0.1:8701"
exec "$ROOT/scripts/start.sh"
