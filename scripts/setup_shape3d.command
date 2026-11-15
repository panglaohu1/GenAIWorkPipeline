#!/bin/zsh
# 双击：安装本地「图生 3D」环境（Hunyuan3D-2mini Turbo，仅形体，跑在 Apple 芯片 GPU / MPS 上）。可重复执行。
#   envs/shape3d       独立 Python 3.12 环境（torch + diffusers + transformers + pymeshlab …）
#   vendor/Hunyuan3D-2 腾讯官方推理代码（只用 hy3dgen.shapegen）
#   models/shape3d     模型权重约 4.2 GB（DiT turbo 3.8 GB + FlashVDM VAE 0.4 GB），用项目自带的分片下载器
# 输出同时写到 logs/setup_shape3d.log。最后做一次自检生成，成功后窗口会显示「完成」。
set -eu
ROOT="${0:A:h:h}"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"
# 从 Finder 双击启动时终端里没有代理环境变量：读取系统代理（如 Clash）供 git 和 HF API 使用；模型大文件仍按下载器默认直连 CDN
if [ -z "${https_proxy:-}" ] && scutil --proxy | grep -q "HTTPSEnable : 1"; then
  PH=$(scutil --proxy | awk '/HTTPSProxy/ {print $3}'); PP=$(scutil --proxy | awk '/HTTPSPort/ {print $3}')
  export https_proxy="http://$PH:$PP" http_proxy="http://$PH:$PP" all_proxy="http://$PH:$PP"
  echo "使用系统代理 $https_proxy"
fi
mkdir -p logs
exec > >(tee -a logs/setup_shape3d.log) 2>&1
echo "=== setup_shape3d $(date) ==="

echo "==> 1/4 Python 环境 envs/shape3d"
[ -x envs/shape3d/bin/python ] || uv venv envs/shape3d --python 3.12
P=envs/shape3d/bin/python
uv pip install --python $P torch torchvision "diffusers>=0.30" "transformers>=4.48,<5" accelerate einops omegaconf \
  "numpy<2.3" scikit-image scipy trimesh pymeshlab opencv-python-headless pillow tqdm pyyaml safetensors

echo "==> 2/4 推理代码 vendor/Hunyuan3D-2"
if [ ! -f vendor/Hunyuan3D-2/setup.py ]; then
  rm -rf vendor/Hunyuan3D-2.tmp
  # 经代理时 GitHub 的 HTTP/2 偶尔断帧：强制 HTTP/1.1，失败再改用源码压缩包
  if ! git -c http.version=HTTP/1.1 clone --depth 1 https://github.com/Tencent-Hunyuan/Hunyuan3D-2 vendor/Hunyuan3D-2.tmp; then
    rm -rf vendor/Hunyuan3D-2.tmp && mkdir -p vendor/Hunyuan3D-2.tmp
    curl -L --http1.1 --retry 5 https://codeload.github.com/Tencent-Hunyuan/Hunyuan3D-2/tar.gz/refs/heads/main \
      | tar -xz --strip-components 1 -C vendor/Hunyuan3D-2.tmp
  fi
  mv vendor/Hunyuan3D-2.tmp vendor/Hunyuan3D-2
fi
uv pip install --python $P --no-deps -e vendor/Hunyuan3D-2

echo "==> 3/4 模型权重 models/shape3d（约 4.2 GB，可断点续传）"
# 先按默认方式（CDN 直连）下载；若 3 分钟没有任何进展，改为 CDN 也走代理再试
python3 scripts/fetch_models.py shape3d & FP=$!
sleep 180
if kill -0 $FP 2>/dev/null && [ "$(du -sk models/shape3d | awk '{print $1}')" -lt 20000 ]; then
  echo "CDN 直连没有进展，改为经代理下载"
  kill $FP; wait $FP 2>/dev/null || true
  FETCH_CDN_PROXY=1 python3 scripts/fetch_models.py shape3d
else
  wait $FP
fi

echo "==> 4/4 自检：用官方示例图生成一个形体"
HY3DGEN_MODELS="$ROOT/models/shape3d" PYTORCH_ENABLE_MPS_FALLBACK=1 \
  $P src/genai_pipeline/shape3d_server.py --selftest vendor/Hunyuan3D-2/assets/demo.png --out models/shape3d/_selftest.glb
echo "完成。可以关闭这个窗口。"
