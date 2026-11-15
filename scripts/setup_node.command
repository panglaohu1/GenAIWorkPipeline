#!/bin/zsh
# 双击：安装 Node.js 到 envs/node（只给管线的能力层用：调 TigerInBamboo 的能力代码）。可重复执行。
# 官方二进制（nodejs.org），按官方 SHASUMS256.txt 校验后再解压；不改系统、不装全局包。
set -eu
ROOT="${0:A:h:h}"
cd "$ROOT"
VER="v22.11.0"
ARCH=$(uname -m); [ "$ARCH" = "arm64" ] || ARCH="x64"
NAME="node-$VER-darwin-$ARCH"
if [ -z "${https_proxy:-}" ] && scutil --proxy | grep -q "HTTPSEnable : 1"; then
  PH=$(scutil --proxy | awk '/HTTPSProxy/ {print $3}'); PP=$(scutil --proxy | awk '/HTTPSPort/ {print $3}')
  export https_proxy="http://$PH:$PP" http_proxy="http://$PH:$PP"
  echo "使用系统代理 $https_proxy"
fi
mkdir -p logs envs
exec > >(tee -a logs/setup_node.log) 2>&1
echo "=== setup_node $(date) ==="
if [ -x envs/node/bin/node ] && [ "$(envs/node/bin/node -v)" = "$VER" ]; then
  echo "已安装 $(envs/node/bin/node -v)"
else
  TMP=$(mktemp -d)
  curl -fsSL "https://nodejs.org/dist/$VER/$NAME.tar.gz" -o "$TMP/$NAME.tar.gz"
  curl -fsSL "https://nodejs.org/dist/$VER/SHASUMS256.txt" -o "$TMP/SHASUMS256.txt"
  (cd "$TMP" && grep " $NAME.tar.gz\$" SHASUMS256.txt | shasum -a 256 -c -)
  rm -rf envs/node.new && mkdir -p envs/node.new
  tar xzf "$TMP/$NAME.tar.gz" -C envs/node.new --strip-components 1
  [ -d envs/node ] && mv envs/node "envs/node.old.$(date +%s)"
  mv envs/node.new envs/node
  rm -rf "$TMP"
fi
echo "==> 自检：加载 TigerInBamboo 能力目录"
.venv/bin/python - <<'PY'
import asyncio, sys
sys.path.insert(0, "src")
from genai_pipeline.capabilities import bridge
ok, why = bridge.available()
print("Node：", why if ok else "不可用 —— " + why)
if ok:
    d = asyncio.run(bridge.call("describe", {}))
    for k, v in d.items():
        print(f"  {k}：", "、".join(x["id"] for x in v) if isinstance(v, list) else v)
PY
echo "完成。可以关闭这个窗口。"
