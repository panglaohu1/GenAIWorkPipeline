#!/bin/zsh
# 双击：安装「图片 → 度量深度 + 相机」环境，并在海岛目标图上验证物件定位。可重复执行。
#   envs/depth                    Python 3.12（torch MPS）
#   vendor/MoGe                   微软 MoGe 代码（只用 MoGe-2；MoGe-3 依赖的 FlexGEMM 不支持 macOS，不装）
#   vendor/Depth-Anything-3       字节 Depth Anything 3 代码（度量版；xformers 不支持 macOS，不装）
#   models/vision/moge-2-vitl-normal、models/vision/da3metric-large   权重约 2.7 GB
# 顺带：重新跑一次模型备份脚本（Bonsai 上次因外接盘自动生成的隐藏文件导致计数不一致而保留）。
ROOT="${0:A:h:h}"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"
if [ -z "${https_proxy:-}" ] && scutil --proxy | grep -q "HTTPSEnable : 1"; then
  PH=$(scutil --proxy | awk '/HTTPSProxy/ {print $3}'); PP=$(scutil --proxy | awk '/HTTPSPort/ {print $3}')
  export https_proxy="http://$PH:$PP" http_proxy="http://$PH:$PP" all_proxy="http://$PH:$PP"
fi
mkdir -p logs
exec > >(tee -a logs/setup_depth.log) 2>&1
echo "=== setup_depth $(date) ==="
[ -d models/llm/bonsai2-27b-mlx ] && zsh scripts/move_models_to_backup.command

clone() {  # $1 仓库  $2 目录
  [ -d "$2/.git" ] || [ -f "$2/pyproject.toml" ] && return 0
  rm -rf "$2.tmp"
  if ! git -c http.version=HTTP/1.1 clone --depth 1 "https://github.com/$1" "$2.tmp"; then
    rm -rf "$2.tmp" && mkdir -p "$2.tmp"
    curl -L --http1.1 --retry 5 "https://codeload.github.com/$1/tar.gz/refs/heads/main" | tar -xz --strip-components 1 -C "$2.tmp"
  fi
  mv "$2.tmp" "$2"
}
echo "==> 1/4 环境 envs/depth"
[ -x envs/depth/bin/python ] || uv venv envs/depth --python 3.12
P=envs/depth/bin/python
uv pip install --python $P torch torchvision "numpy<2.3" scipy opencv-python-headless pillow trimesh huggingface_hub \
  click tqdm requests matplotlib einops omegaconf imageio safetensors plyfile typer \
  "utils3d_moge @ git+https://github.com/EasternJournalist/utils3d-moge.git@62f09d58509485564e24d5d9f6aac9ee9ebc0c37" \
  "pipeline @ git+https://github.com/EasternJournalist/pipeline.git@1c511390d90226c00c101f34b84df26a0f8789b4" \
  "moviepy==1.0.3" pycolmap evo e3nn addict pillow_heif fastapi pydantic scikit-learn
echo "==> 2/4 代码"
clone microsoft/MoGe vendor/MoGe
clone ByteDance-Seed/Depth-Anything-3 vendor/Depth-Anything-3
echo "==> 3/4 权重（约 2.7 GB）"
python3 scripts/fetch_models.py depth || FETCH_CDN_PROXY=1 python3 scripts/fetch_models.py depth
echo "==> 4/4 海岛目标图验证"
IMG=runs/20261016-065059-1927/concept/_scene/target.png
PYTORCH_ENABLE_MPS_FALLBACK=1 $P scripts/depth_layout_probe.py $IMG runs/_depth_probe/moge2 --model moge2
PYTORCH_ENABLE_MPS_FALLBACK=1 $P scripts/depth_layout_probe.py $IMG runs/_depth_probe/da3 --model da3 || echo "DA3 未跑通（见上方报错）"
echo "完成。可以关闭这个窗口。"
