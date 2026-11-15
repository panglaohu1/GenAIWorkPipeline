#!/bin/zsh
# 双击：阶段 1 验收。重新打包安装本地 fork → 在 Blender 里用原版作业链路调用本机 FLUX（及 Hunyuan3D）
# → 再起无界面 MCP 列出本地模型。同一时间只跑一份；记录峰值内存。
ROOT="${0:A:h:h}"
cd "$ROOT"
mkdir -p logs build/scenario_local envs/blender_user
LOCK=logs/.scenario_phase1.lock
if ! mkdir "$LOCK" 2>/dev/null; then echo "已有一份在跑（$LOCK），这次不启动。"; exit 0; fi
BPID=""
cleanup() { [[ -n "$BPID" ]] && kill "$BPID" 2>/dev/null; rmdir "$LOCK"; }
trap cleanup EXIT
exec > >(tee -a logs/scenario_phase1.log) 2>&1
echo "=== scenario phase1 $(date) ==="
B=/Applications/Blender.app/Contents/MacOS/Blender
PY="$ROOT/envs/depth/bin/python"
export BLENDER_USER_RESOURCES="$ROOT/envs/blender_user"
export GENAI_SCENE_STATE="$ROOT/build/scenario_local/state"
rm -f build/scenario_local/genai_scene_stage-*.zip
"$B" --background --factory-startup --command extension build --source-dir vendor/scenario-local/scenario --output-dir build/scenario_local || exit 1
ZIP=$(ls -t build/scenario_local/genai_scene_stage-*.zip | head -1)
"$B" --background --command extension install-file -r user_default --enable "$ZIP" || exit 1
echo "--- local job smoke (real services)"
OUT=build/scenario_local/smoke_$(date +%H%M%S)
/usr/bin/time -l "$B" --background --python scripts/scenario_local/local_job_smoke.py -- "$OUT" 2>&1 \
  | grep -v "^ *[0-9]* *\(average\|involuntary\|voluntary\|signals\|messages\|block\|page\|swaps\|instructions\|cycles\)"
echo "--- headless MCP list_models"
PORT=9886
"$B" --background --command genai_scene --port $PORT > logs/scenario_phase1_server.log 2>&1 &
BPID=$!
for i in {1..60}; do grep -q "MCP server on" logs/scenario_phase1_server.log && break; sleep 1; done
U=http://127.0.0.1:$PORT/mcp
H=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
curl -s -m 10 -X POST $U $H -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"phase1","version":"0"}}}' > /dev/null
curl -s -m 30 -X POST $U $H -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_models","arguments":{"lane":"image"}}}' > build/scenario_local/list_models_image.json
curl -s -m 30 -X POST $U $H -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_models","arguments":{"lane":"3d"}}}' > build/scenario_local/list_models_3d.json
head -c 700 build/scenario_local/list_models_image.json; echo; head -c 500 build/scenario_local/list_models_3d.json; echo
kill -INT $BPID 2>/dev/null; sleep 3; kill $BPID 2>/dev/null; BPID=""
echo "完成。可以关闭这个窗口。"
