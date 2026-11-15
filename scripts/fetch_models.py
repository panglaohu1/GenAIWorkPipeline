#!/usr/bin/env python3
"""多连接、可续传、带 SHA256 校验的 Hugging Face 模型下载器（仅标准库 + curl）。

为什么不用 `hf download`：本机经代理访问 HF CDN 仅 ~0.02 MB/s 且 xet 会断流。
本脚本通过代理访问 huggingface.co API（元数据），CDN 文件直连并按分片并行下载。

用法:
  python3 scripts/fetch_models.py            # 按优先级下载全部
  python3 scripts/fetch_models.py bonsai flux
  python3 scripts/fetch_models.py --status   # 查看进度（控制台也会读取 models/.fetch-status.json）
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
STATUS_FILE = MODELS / ".fetch-status.json"
HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
CHUNK = 64 * 1024 * 1024
CONNS = int(os.environ.get("FETCH_CONNS", "8"))
# CDN 默认直连；设 FETCH_CDN_PROXY=1 则 CDN 也走系统代理
CDN_DIRECT = os.environ.get("FETCH_CDN_PROXY", "0") != "1"

# 名称 -> (repo, 目标目录, include 通配符列表或 None 表示全部)
PLAN: dict[str, list[tuple[str, str, list[str] | None]]] = {
    "bonsai": [("prism-ml/Ternary-Bonsai-2-27B-mlx-2bit", "llm/bonsai2-27b-mlx", None)],
    "flux": [("dhairyashil/FLUX.1-schnell-mflux-4bit", "image/flux1-schnell-mflux-4bit", None)],
    "qwen-image": [
        ("leejet/Qwen-Image-2.1-GGUF", "image/qwen-image-2.1", ["qwen_image_2.1-Q6_K.gguf"]),
        ("Comfy-Org/Qwen-Image-2.1", "image/qwen-image-2.1", ["vae/qwen_image_2.1_vae_bf16.safetensors"]),
        ("Qwen/Qwen3-VL-8B-Instruct-GGUF", "image/qwen-image-2.1",
         ["Qwen3VL-8B-Instruct-Q4_K_M.gguf", "mmproj-Qwen3VL-8B-Instruct-F16.gguf"]),
    ],
    "qwen-fallback": [("mlx-community/Qwen3.8-27B-oQ6", "llm/qwen3.8-27b-oq6", None)],
    "laya": [("convaiinnovations/laya-multilingual", "decision/laya-multilingual", None)],
    # 客观评分：渲染图 vs 目标图的图像特征相似度（免授权；DINOv3 需 HF 账号申请，暂用 DINOv2）
    "gemma31": [("mlx-community/gemma-4-31b-it-4bit", "llm/gemma-4-31b-it-4bit", None)],  # 建模 A/B 候选
    # 建模 A/B 候选：Sage-A1.5（上海 AI 实验室，35B MoE、每 token 约 3B 激活）Q4_K_M GGUF 21.7 GB + 视觉投影 0.9 GB，llama.cpp 运行
    "sage": [("InternScience/Sage-A1.5-Q4_K_M-GGUF", "llm/sage-a1.5-gguf",
              ["Sage-A1.5-Q4_K_M.gguf", "Sage-A1.5-mmproj.gguf"])],
    "sam3": [("mlx-community/sam3-image", "vision/sam3-image", None)],  # SAM 3 MLX：文字提示实例分割
    # 图生 3D（只取形体）：Hunyuan3D-2mini Turbo 的 DiT 与 FlashVDM 加速 VAE，约 4.2 GB；目录结构与 hy3dgen 的 HY3DGEN_MODELS 约定一致
    "shape3d": [("tencent/Hunyuan3D-2mini", "shape3d/tencent/Hunyuan3D-2mini",
                 ["hunyuan3d-dit-v2-mini-turbo/config.yaml", "hunyuan3d-dit-v2-mini-turbo/model.fp16.safetensors",
                  "hunyuan3d-vae-v2-mini-turbo/config.yaml", "hunyuan3d-vae-v2-mini-turbo/model.fp16.safetensors"])],
    # 图像 → 度量深度 + 相机（布局顾问验证）：MoGe-2（MIT）与 Depth Anything 3 度量版（Apache-2.0）
    "depth": [("Ruicheng/moge-2-vitl-normal", "vision/moge-2-vitl-normal", None),
              ("depth-anything/DA3METRIC-LARGE", "vision/da3metric-large", None)],
    "vision-metrics": [("google/siglip2-base-patch16-224", "vision/siglip2-base-patch16-224", None),
                       ("facebook/dinov2-base", "vision/dinov2-base", ["config.json", "model.safetensors",
                                                                        "preprocessor_config.json"])],
}
ORDER = ["bonsai", "flux", "qwen-fallback", "laya"]  # qwen-image 已删除，需要时单独指定下载

_status_lock = threading.Lock()
_status: dict = {}


def save_status(**kw) -> None:
    with _status_lock:
        _status.update(kw, updated=time.time())
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_status, ensure_ascii=False, indent=1))
        tmp.replace(STATUS_FILE)


def api_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as r:  # 走系统代理
        return json.load(r)


def list_files(repo: str, include: list[str] | None) -> list[dict]:
    files = api_json(f"{HF}/api/models/{repo}/tree/main?recursive=true")
    out = []
    for f in files:
        if f["type"] != "file":
            continue
        if include and not any(fnmatch(f["path"], p) for p in include):
            continue
        out.append({"path": f["path"], "size": f.get("size", 0),
                    "sha256": (f.get("lfs") or {}).get("oid")})
    return out


def resolve_cdn(repo: str, path: str) -> str:
    """HEAD 获取 302 跳转到的 CDN 地址（签名 URL，有效期数小时）。"""
    url = f"{HF}/{repo}/resolve/main/{urllib.parse.quote(path)}"
    r = subprocess.run(["curl", "-sI", "-m", "30", url], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.lower().startswith("location:"):
            loc = line.split(":", 1)[1].strip()
            return urllib.parse.urljoin(url, loc)
    return url


def fetch_range(urlref: dict, dst: Path, start: int, end: int) -> None:
    want = end - start + 1
    for attempt in range(30):
        if attempt and attempt % 5 == 0:  # 签名 URL 可能过期，定期重新解析
            urlref["url"] = resolve_cdn(urlref["repo"], urlref["path"])
        url = urlref["url"]
        have = dst.stat().st_size if dst.exists() else 0
        if have >= want:
            return
        cmd = ["curl", "-sfL", "--connect-timeout", "20", "--speed-limit", "20000", "--speed-time", "40",
               "-r", f"{start + have}-{end}", "-o", "-", url]
        if CDN_DIRECT:
            cmd[1:1] = ["--noproxy", "*"]
        with open(dst, "ab") as fh:
            subprocess.run(cmd, stdout=fh, stderr=subprocess.DEVNULL)
        time.sleep(min(2 * attempt, 20))
    raise RuntimeError(f"range {start}-{end} failed after retries")


def download(repo: str, target: Path, f: dict, progress: dict) -> None:
    dst = target / f["path"]
    done_marker = dst.with_name(dst.name + ".ok")
    if dst.exists() and done_marker.exists() and dst.stat().st_size == f["size"]:
        progress["done"] += f["size"]
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    size = f["size"]
    if not f["sha256"]:  # 非 LFS 小文件（json/模板等）走 API 代理下载
        urllib.request.urlretrieve(f"{HF}/{repo}/resolve/main/{urllib.parse.quote(f['path'])}", dst)
    else:
        urlref = {"repo": repo, "path": f["path"], "url": resolve_cdn(repo, f["path"])}
        parts_dir = dst.with_name(dst.name + ".parts")
        parts_dir.mkdir(exist_ok=True)
        ranges = [(i, s, min(s + CHUNK, size) - 1) for i, s in enumerate(range(0, size, CHUNK))]

        def job(r):
            i, s, e = r
            p = parts_dir / f"{i:05d}"
            fetch_range(urlref, p, s, e)
            with _status_lock:
                progress["done"] += e - s + 1

        with ThreadPoolExecutor(CONNS) as ex:
            list(ex.map(job, ranges))
        h = hashlib.sha256()
        with open(dst, "wb") as out:
            for i, _, _ in ranges:
                data = (parts_dir / f"{i:05d}").read_bytes()
                h.update(data)
                out.write(data)
        if h.hexdigest() != f["sha256"]:
            dst.unlink()
            for p in parts_dir.iterdir():
                p.unlink()
            raise RuntimeError(f"sha256 mismatch: {f['path']}")
        for p in parts_dir.iterdir():
            p.unlink()
        parts_dir.rmdir()
    if dst.stat().st_size != size:
        raise RuntimeError(f"size mismatch: {f['path']}")
    done_marker.touch()
    if not f["sha256"]:
        progress["done"] += size


def fetch_group(name: str) -> None:
    for repo, sub, include in PLAN[name]:
        target = MODELS / sub
        files = list_files(repo, include)
        total = sum(f["size"] for f in files)
        progress = {"done": 0}
        stop = threading.Event()

        def reporter():
            last, lt = 0, time.time()
            while not stop.wait(5):
                now = time.time()
                # 以磁盘实际字节为准（含未完成分片），组装时可能短暂重复计数
                done = 0
                for f in files:  # 只统计本仓库的文件（多个仓库可能共用一个目录）
                    dst = target / f["path"]
                    parts = dst.with_name(dst.name + ".parts")
                    if parts.is_dir():
                        done += sum(x.stat().st_size for x in parts.iterdir())
                    elif dst.exists():
                        done += dst.stat().st_size
                progress["done"] = min(total, done)
                rate = (progress["done"] - last) / (now - lt)
                last, lt = progress["done"], now
                save_status(**{name: {"state": "downloading", "repo": repo, "done": progress["done"],
                                      "total": total, "mbps": round(rate / 1e6, 2)}})
                print(f"[{name}] {progress['done'] / 1e9:.2f}/{total / 1e9:.2f} GB  {rate / 1e6:.2f} MB/s", flush=True)

        th = threading.Thread(target=reporter, daemon=True)
        th.start()
        try:
            for f in sorted(files, key=lambda x: x["size"]):
                download(repo, target, f, progress)
        finally:
            stop.set()
    save_status(**{name: {"state": "done"}})
    print(f"[{name}] done", flush=True)


def main(argv: list[str]) -> int:
    if "--status" in argv:
        print(STATUS_FILE.read_text() if STATUS_FILE.exists() else "{}")
        return 0
    if STATUS_FILE.exists():
        _status.update(json.loads(STATUS_FILE.read_text()))
    groups = [a for a in argv if not a.startswith("-")] or ORDER
    rc = 0
    for g in groups:
        try:
            fetch_group(g)
        except Exception as e:  # 继续下一个组，状态文件记录失败原因
            rc = 1
            save_status(**{g: {"state": "failed", "error": str(e)}})
            print(f"[{g}] FAILED: {e}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
