#!/bin/zsh
# 双击：安装本地「音效」环境（MOSS-SoundEffect，OpenMOSS，Apache-2.0；MLX 4-bit，跑在 Apple 芯片 GPU 上）。可重复执行。
#   envs/audio        独立 Python 3.13 环境（mlx-speech）
#   models/audio/hf   模型权重（Hugging Face 缓存，首次自检时下载；之后服务离线加载）
# 输出同时写到 logs/setup_audio.log。最后做一次自检生成，成功后窗口会显示「完成」。
set -eu
ROOT="${0:A:h:h}"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"
# 从 Finder 双击启动时终端里没有代理环境变量：读取系统代理（如 Clash）供 pip 和 HF 下载使用
if [ -z "${https_proxy:-}" ] && scutil --proxy | grep -q "HTTPSEnable : 1"; then
  PH=$(scutil --proxy | awk '/HTTPSProxy/ {print $3}'); PP=$(scutil --proxy | awk '/HTTPSPort/ {print $3}')
  export https_proxy="http://$PH:$PP" http_proxy="http://$PH:$PP" all_proxy="http://$PH:$PP"
  echo "使用系统代理 $https_proxy"
fi
mkdir -p logs models/audio/hf
exec > >(tee -a logs/setup_audio.log) 2>&1
echo "=== setup_audio $(date) ==="

echo "==> 1/3 Python 环境 envs/audio（Python 3.13）"
[ -x envs/audio/bin/python ] || uv venv envs/audio --python 3.13
P=envs/audio/bin/python
uv pip install --python $P mlx-speech numpy "huggingface_hub[hf_xet]"

echo "==> 2/3 下载模型并自检（首次约 1–2 GB）"
export HF_HOME="$ROOT/models/audio/hf"
HF_HUB_OFFLINE=0 $P src/genai_pipeline/audio_server.py --selftest "gentle ocean waves lapping on rocks, distant seagulls" \
  --seconds 6 --out models/audio/_selftest.wav

echo "==> 3/3 离线复检（不联网也能加载）"
HF_HUB_OFFLINE=1 $P src/genai_pipeline/audio_server.py --selftest "light wind over grass" --seconds 4 --out models/audio/_selftest2.wav
touch models/audio/hf/.ready
echo "完成。可以关闭这个窗口。"
