#!/bin/zsh
# 双击：重启控制台（结束占用 8701 端口的旧进程，再用新代码启动）。Python 代码更新后需要这样重启一次。
ROOT="${0:A:h:h}"
PIDS=$(lsof -ti tcp:8701 -sTCP:LISTEN)
if [ -n "$PIDS" ]; then
  echo "结束旧控制台进程：$PIDS"
  kill ${=PIDS}; sleep 2
  kill -9 $(lsof -ti tcp:8701 -sTCP:LISTEN) 2>/dev/null
fi
echo "启动控制台…（浏览器打开 http://127.0.0.1:8701 ）"
exec "$ROOT/scripts/start.sh"
