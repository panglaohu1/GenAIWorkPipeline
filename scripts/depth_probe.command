#!/bin/zsh
# 双击：只跑海岛定位验证（不重装环境）。同一时间只允许一份在跑；每一步记录峰值内存（/usr/bin/time -l）。
ROOT="${0:A:h:h}"
cd "$ROOT"
mkdir -p logs
LOCK=logs/.depth_probe.lock
if ! mkdir "$LOCK" 2>/dev/null; then echo "已有一份验证在跑（$LOCK），这次不启动。"; exit 0; fi
trap 'rmdir "$LOCK"' EXIT
exec > >(tee -a logs/depth_probe.log) 2>&1
echo "=== depth_probe $(date) ==="
IMG=runs/20261016-065059-1927/concept/_scene/target.png
P=envs/depth/bin/python
for M in moge2 da3; do
  echo "--- $M"
  KMP_DUPLICATE_LIB_OK=TRUE PYTORCH_ENABLE_MPS_FALLBACK=1 /usr/bin/time -l $P scripts/depth_layout_probe.py $IMG runs/_depth_probe/$M --model $M 2>&1 \
    | grep -v "^ *[0-9]* *\(average\|involuntary\|voluntary\|signals\|messages\|block\|page\|swaps\|instructions\|cycles\)" 
done
echo "完成。可以关闭这个窗口。"
