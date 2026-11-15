#!/bin/zsh
# 双击：配置建模候选模型 Sage-A1.5（llama.cpp 运行 Q4_K_M GGUF）。可重复执行、断点续传。
#   vendor/llama.cpp          llama.cpp 官方 macOS arm64 预编译包（含 llama-server）
#   models/llm/sage-a1.5-gguf  Sage-A1.5-Q4_K_M.gguf 21.7 GB + Sage-A1.5-mmproj.gguf 0.9 GB（用项目自带的分片下载器）
# 输出同时写到 logs/setup_sage.log。
set -eu
ROOT="${0:A:h:h}"
cd "$ROOT"
if [ -z "${https_proxy:-}" ] && scutil --proxy | grep -q "HTTPSEnable : 1"; then
  PH=$(scutil --proxy | awk '/HTTPSProxy/ {print $3}'); PP=$(scutil --proxy | awk '/HTTPSPort/ {print $3}')
  export https_proxy="http://$PH:$PP" http_proxy="http://$PH:$PP" all_proxy="http://$PH:$PP"
  echo "使用系统代理 $https_proxy"
fi
mkdir -p logs vendor
exec > >(tee -a logs/setup_sage.log) 2>&1
echo "=== setup_sage $(date) ==="

echo "==> 1/3 llama.cpp 预编译包 vendor/llama.cpp"
if [ -z "$(find vendor/llama.cpp -name llama-server -type f 2>/dev/null | head -1)" ]; then
  # 固定版本（2026-10 的 nightly，支持 Sage-A1.5 的 qwen35moe 架构）；GitHub API 经代理常被限流，不用它查“最新”
  URL="https://github.com/ggml-org/llama.cpp/releases/download/b11669/llama-b11669-bin-macos-arm64.tar.gz"
  echo "下载 $URL"
  rm -rf vendor/llama.cpp.tmp && mkdir -p vendor/llama.cpp.tmp
  curl -L --http1.1 --retry 5 -o vendor/llama.cpp.tmp/pkg "$URL"
  case "$URL" in
    *.zip) (cd vendor/llama.cpp.tmp && unzip -q pkg) ;;
    *) tar -xzf vendor/llama.cpp.tmp/pkg -C vendor/llama.cpp.tmp ;;
  esac
  mv vendor/llama.cpp.tmp/pkg "vendor/llama.cpp.tmp/$(basename "$URL")"
  [ -e vendor/llama.cpp ] && mv vendor/llama.cpp "vendor/llama.cpp.old.$(date +%s)"
  mv vendor/llama.cpp.tmp vendor/llama.cpp
  xattr -dr com.apple.quarantine vendor/llama.cpp 2>/dev/null || true
fi
LS=$(find vendor/llama.cpp -name llama-server -type f | head -1)
chmod +x "$LS"
"$LS" --version 2>&1 | head -3

echo "==> 2/3 模型权重 models/llm/sage-a1.5-gguf（约 22.6 GB，可断点续传）"
python3 scripts/fetch_models.py sage & FP=$!
sleep 180
if kill -0 $FP 2>/dev/null && [ "$(du -sk models/llm/sage-a1.5-gguf 2>/dev/null | awk '{print $1}')" -lt 50000 ]; then
  echo "CDN 直连没有进展，改为经代理下载"
  kill $FP; wait $FP 2>/dev/null || true
  FETCH_CDN_PROXY=1 python3 scripts/fetch_models.py sage
else
  wait $FP
fi

echo "==> 3/3 自检：加载模型并回答一句话（端口 8095，测完即关）"
"$LS" -m models/llm/sage-a1.5-gguf/Sage-A1.5-Q4_K_M.gguf --mmproj models/llm/sage-a1.5-gguf/Sage-A1.5-mmproj.gguf \
  --host 127.0.0.1 --port 8095 -c 4096 -ngl 999 -np 1 --jinja --reasoning-format deepseek > logs/sage_selftest_server.log 2>&1 & SP=$!
for i in $(seq 1 120); do curl -sf --noproxy '*' http://127.0.0.1:8095/health >/dev/null && break; sleep 2; done
T0=$(date +%s)
curl -s --noproxy '*' http://127.0.0.1:8095/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"用一句话说明：六边形格子为什么适合做程序化地形？只输出 JSON {\"answer\": \"...\"}"}],"max_tokens":800,"chat_template_kwargs":{"enable_thinking":false}}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('回答：', d['choices'][0]['message'].get('content')); print('用量：', d.get('usage'), d.get('timings',{}).get('predicted_per_second'), 'tok/s')"
echo "耗时 $(( $(date +%s) - T0 )) 秒"
kill $SP; wait $SP 2>/dev/null || true
echo "完成。可以关闭这个窗口。"
