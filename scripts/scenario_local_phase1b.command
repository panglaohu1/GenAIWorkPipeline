#!/bin/zsh
# 双击：阶段 1 补充验收。重新打包安装本地 fork → 在 Blender 里用原版作业链路：
# 上传参考图（分段写进本地目录）→ 用上传的图做图生 3D（本机 Hunyuan3D）→ 翻译与 Prompt Spark（本机 LLM）。
# 同一时间只跑一份；记录峰值内存。
ROOT="${0:A:h:h}"
cd "$ROOT"
mkdir -p logs build/scenario_local envs/blender_user
LOCK=logs/.scenario_phase1b.lock
if ! mkdir "$LOCK" 2>/dev/null; then echo "已有一份在跑（$LOCK），这次不启动。"; exit 0; fi
trap 'rmdir "$LOCK"' EXIT
exec > >(tee -a logs/scenario_phase1b.log) 2>&1
echo "=== scenario phase1b $(date) ==="
B=/Applications/Blender.app/Contents/MacOS/Blender
export BLENDER_USER_RESOURCES="$ROOT/envs/blender_user"
export GENAI_SCENE_STATE="$ROOT/build/scenario_local/state"
mv -f build/scenario_local/genai_scene_stage-*.zip build/scenario_local/old_zips/ 2>/dev/null
mkdir -p build/scenario_local/old_zips
"$B" --background --factory-startup --command extension build --source-dir vendor/scenario-local/scenario --output-dir build/scenario_local || exit 1
ZIP=$(ls -t build/scenario_local/genai_scene_stage-*.zip | head -1)
"$B" --background --command extension install-file -r user_default --enable "$ZIP" || exit 1
REF="${1:-$ROOT/runs/20261023-215600-151a/references/island_target.png}"
OUT=build/scenario_local/smoke1b_$(date +%H%M%S)
/usr/bin/time -l "$B" --background --python scripts/scenario_local/local_upload_text_smoke.py -- "$OUT" "$REF" 2>&1 \
  | grep -v "^ *[0-9]* *\(average\|involuntary\|voluntary\|signals\|messages\|block\|page\|swaps\|instructions\|cycles\)"
echo "完成。可以关闭这个窗口。"
