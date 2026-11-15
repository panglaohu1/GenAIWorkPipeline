#!/bin/zsh
# 双击：Sage-A1.5 下载停滞时用——停掉直连 CDN 的下载进程，改为经系统代理续传（已下的分片保留），下完后跑自检。
ROOT="${0:A:h:h}"
cd "$ROOT"
if [ -z "${https_proxy:-}" ] && scutil --proxy | grep -q "HTTPSEnable : 1"; then
  PH=$(scutil --proxy | awk '/HTTPSProxy/ {print $3}'); PP=$(scutil --proxy | awk '/HTTPSPort/ {print $3}')
  export https_proxy="http://$PH:$PP" http_proxy="http://$PH:$PP" all_proxy="http://$PH:$PP"
fi
exec > >(tee -a logs/setup_sage.log) 2>&1
echo "=== resume_sage $(date)：改为经代理续传 ==="
pkill -f "fetch_models.py sage" || true
sleep 3
FETCH_CDN_PROXY=1 python3 scripts/fetch_models.py sage && zsh scripts/setup_sage.command
