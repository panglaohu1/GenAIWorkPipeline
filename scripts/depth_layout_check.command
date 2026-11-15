#!/bin/zsh
# 双击：对最近一次拆分保存的 request.json 单独跑一次深度定位（排错用），一次只跑一份，记录峰值内存。
ROOT="${0:A:h:h}"
cd "$ROOT"
mkdir -p logs
LOCK=logs/.depth_layout_check.lock
if ! mkdir "$LOCK" 2>/dev/null; then echo "已有一份在跑（$LOCK）"; exit 0; fi
trap 'rmdir "$LOCK"' EXIT
exec > >(tee -a logs/depth_layout_check.log) 2>&1
REQ=$(ls -t runs/*/concept/_scene/depth/request.json | head -1)
echo "=== depth_layout_check $(date) $REQ ==="
KMP_DUPLICATE_LIB_OK=TRUE PYTORCH_ENABLE_MPS_FALLBACK=1 /usr/bin/time -l envs/depth/bin/python src/genai_pipeline/depth_layout.py "$REQ" "${REQ:h}/result.json" 2>&1 | grep -v "^ *[0-9]* *\(average\|involuntary\|voluntary\|signals\|messages\|block\|page\|swaps\|instructions\|cycles\)"
echo "exit=$pipestatus[1]"
ls -la "${REQ:h}"
echo "完成。可以关闭这个窗口。"
