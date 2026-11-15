#!/bin/zsh
# 双击：阶段 0 冒烟测试。打包 vendor/scenario-local → 装进隔离的 Blender 用户目录 envs/blender_user（不动你自己的 Blender 设置）
# → 以 --offline-mode 无界面启动本地 MCP（127.0.0.1:9886）→ 列工具、调 scene_summary → 关掉。同一时间只跑一份，记录峰值内存。
ROOT="${0:A:h:h}"
cd "$ROOT"
mkdir -p logs build/scenario_local envs/blender_user
LOCK=logs/.scenario_phase0.lock
if ! mkdir "$LOCK" 2>/dev/null; then echo "已有一份在跑（$LOCK），这次不启动。"; exit 0; fi
BPID=""
cleanup() { [[ -n "$BPID" ]] && kill "$BPID" 2>/dev/null; rmdir "$LOCK"; }
trap cleanup EXIT
exec > >(tee -a logs/scenario_phase0.log) 2>&1
echo "=== scenario phase0 $(date) ==="
PY="$ROOT/envs/depth/bin/python"
B=/Applications/Blender.app/Contents/MacOS/Blender
export BLENDER_USER_RESOURCES="$ROOT/envs/blender_user"
"$B" --version | head -1
echo "--- build"
"$B" --background --factory-startup --command extension build --source-dir vendor/scenario-local/scenario --output-dir build/scenario_local || exit 1
ZIP=$(ls -t build/scenario_local/genai_scene_stage-*.zip | head -1); echo "zip: $ZIP"
echo "--- install (isolated user dir)"
"$B" --background --command extension install-file -r user_default --enable "$ZIP" || exit 1
echo "--- headless MCP, offline"
PORT=9886
/usr/bin/time -l "$B" --background --offline-mode --command genai_scene --port $PORT > logs/scenario_phase0_server.log 2>&1 &
BPID=$!
for i in {1..60}; do grep -q "MCP server on" logs/scenario_phase0_server.log && break; sleep 1; done
grep "MCP server on\|Error\|error" logs/scenario_phase0_server.log | head -5
U=http://127.0.0.1:$PORT/mcp
H=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
curl -s -m 10 -X POST $U $H -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"phase0","version":"0"}}}' > build/scenario_local/init.json
curl -s -m 10 -X POST $U $H -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' > build/scenario_local/tools.json
TOOL=$($PY -c "import json;t=json.load(open('build/scenario_local/tools.json'))['result']['tools'];print(next(x['name'] for x in t if 'scene_summary' in x['name']))" 2>/dev/null)
echo "tools: $($PY -c "import json;print(len(json.load(open('build/scenario_local/tools.json'))['result']['tools']))" 2>/dev/null)  call: $TOOL"
curl -s -m 20 -X POST $U $H -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$TOOL\",\"arguments\":{}}}" > build/scenario_local/scene_summary.json
head -c 600 build/scenario_local/scene_summary.json; echo
kill -INT $BPID 2>/dev/null; sleep 3; kill $BPID 2>/dev/null; wait $BPID 2>/dev/null; BPID=""
grep "maximum resident\|peak memory" logs/scenario_phase0_server.log
echo "完成。可以关闭这个窗口。"
