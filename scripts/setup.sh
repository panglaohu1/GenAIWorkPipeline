#!/bin/zsh
# 一次性环境安装：uv、三个隔离 Python 环境、stable-diffusion.cpp。可重复执行。
set -eu
ROOT="${0:A:h:h}"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"

command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh

echo "==> 主环境（编排器 / 控制台 / MCP 服务端）"
uv sync --python 3.12 --extra dev

echo "==> 推理环境 envs/llm（mlx-vlm 0.7.2 原生支持 Bonsai 2）"
[ -d vendor/Bonsai-demo ] || git clone --depth 1 https://github.com/PrismML-Eng/Bonsai-demo vendor/Bonsai-demo
[ -x envs/llm/bin/python ] || uv venv envs/llm --python 3.12
uv pip install --python envs/llm/bin/python -r vendor/Bonsai-demo/scripts/requirements-mlx-vlm.txt

echo "==> 生图环境 envs/image（mflux）"
[ -x envs/image/bin/python ] || uv venv envs/image --python 3.12
uv pip install --python envs/image/bin/python mflux

echo "==> stable-diffusion.cpp（Qwen-Image 兜底）"
if [ ! -x vendor/sd.cpp/sd-cli ]; then
  mkdir -p vendor/sd.cpp
  url=$(curl -s https://api.github.com/repos/leejet/stable-diffusion.cpp/releases/latest \
        | python3 -c "import json,sys; print([a['browser_download_url'] for a in json.load(sys.stdin)['assets'] if 'Darwin' in a['name']][0])")
  curl -L "$url" -o vendor/sd.cpp/sd.zip && (cd vendor/sd.cpp && unzip -oq sd.zip && rm sd.zip)
fi
echo "==> 图生 3D 环境 envs/shape3d（Hunyuan3D-2mini Turbo，约 4.2 GB 权重；也可单独双击 scripts/setup_shape3d.command）"
zsh scripts/setup_shape3d.command
echo "完成。下一步: python3 scripts/fetch_models.py && scripts/start.sh"
