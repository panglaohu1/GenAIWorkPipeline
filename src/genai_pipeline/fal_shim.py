"""本地 FAL 兼容服务：让只认 FAL 接口的工具（如 image-blaster）改用本机的 Bonsai + Flux 出图。

不需要、也不产生任何真实密钥：调用方的 FAL_KEY 填任意占位值（例如 local）即可，本服务不校验。
只监听 127.0.0.1。

接口（与 fal 一致的部分）:
  POST /<endpoint>                              队列提交 → {request_id, status_url, response_url}
  GET  /<endpoint>/requests/<id>/status         → {status: IN_QUEUE|IN_PROGRESS|COMPLETED, error?}
  GET  /<endpoint>/requests/<id>                → 结果 JSON（{images:[{url,...}]}）
  POST /run/<endpoint>                          同步执行（对应 https://fal.run/<endpoint>）
  GET  /files/<name>                            生成的图片
  GET  /health

能本地做的：图像编辑 / 文生图类端点（nano-banana*/edit、gpt-image*/edit、flux*、*text-to-image* …）
  1. Bonsai 看输入图 + 编辑指令，写出“编辑后画面”的完整英文描述（Flux 不懂“把椅子去掉”这类指令，只懂描述）；
     Bonsai 不在线时直接用原指令。
  2. Flux（127.0.0.1:8090）以第一张输入图为底图做图生图（strength 默认 0.6）；没有输入图就文生图。
不能本地做的（返回 501 并说明）：图生 3D（hunyuan / meshy / trellis）、音效（elevenlabs）——本机没有对应模型。

用法（在流水线目录）:
  PYTHONPATH=src .venv/bin/python -m genai_pipeline.fal_shim --port 8095
然后把调用方的 FAL 地址指向 http://127.0.0.1:8095（见 docs/使用手册.md「本地 FAL 兼容服务」）。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import threading
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11：读不了配置就用默认端口
    tomllib = None
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CFG: dict = {}
STATE: dict = {"base": "http://127.0.0.1:8095", "out": ROOT / "runs" / "_fal_shim", "flux": "http://127.0.0.1:8090",
               "bonsai": "http://127.0.0.1:8081", "bonsai_model": "", "strength": 0.6}
JOBS: dict[str, dict] = {}
_lock = threading.Lock()

IMAGE_HINTS = ("edit", "image", "flux", "banana", "imagen", "sdxl", "stable-diffusion", "recraft", "ideogram")
NO_LOCAL = {"3d": ("3d", "hunyuan", "meshy", "trellis", "tripo", "rodin"),
            "音效/音频": ("elevenlabs", "sound", "audio", "tts", "music", "speech")}

REWRITE = ("You turn an image-edit instruction into a prompt for a text-to-image model that will repaint the given image. "
           "Look at the image, apply the instruction mentally, and describe the RESULTING picture in one English paragraph "
           "(under 90 words): subject, layout, materials, colours, lighting, camera. Describe only what should be visible; "
           "never write negations such as 'no chair' or 'without'. Output the paragraph only.")


def kind_of(endpoint: str) -> str:
    e = endpoint.lower()
    for k, keys in NO_LOCAL.items():
        if any(x in e for x in keys):
            return k
    return "image" if any(x in e for x in IMAGE_HINTS) else "unknown"


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _fetch_bytes(url: str) -> tuple[bytes, str]:
    m = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", url, re.S)
    if m:
        data = m.group(3)
        raw = base64.b64decode(data) if m.group(2) else urllib.request.unquote(data).encode()
        return raw, m.group(1) or "image/png"
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read(), r.headers.get("Content-Type", "image/png")


def input_images(inp: dict) -> list[str]:
    urls = []
    for k in ("image_urls", "images"):
        v = inp.get(k)
        if isinstance(v, list):
            urls += [x if isinstance(x, str) else (x or {}).get("url", "") for x in v]
    for k in ("image_url", "init_image_url", "image"):
        if isinstance(inp.get(k), str):
            urls.append(inp[k])
    return [u for u in urls if u]


def _size(inp: dict, img_path: Path | None) -> tuple[int, int]:
    """Flux 需要 64 的倍数；优先跟随输入图比例，长边不超过 1024。"""
    w = h = 1024
    size = inp.get("image_size")
    if isinstance(size, dict) and size.get("width"):
        w, h = int(size["width"]), int(size["height"])
    elif img_path is not None:
        try:
            from PIL import Image
            with Image.open(img_path) as im:
                w, h = im.size
        except Exception:
            pass
    s = 1024 / max(w, h)
    return max(256, round(w * s / 64) * 64), max(256, round(h * s / 64) * 64)


def rewrite_prompt(instruction: str, image: bytes | None, mime: str) -> tuple[str, str]:
    """返回 (Flux 提示词, 来源)。Bonsai 不可用时退回原指令。"""
    if image is None or not STATE["bonsai"]:
        return instruction, "instruction"
    content = [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}},
               {"type": "text", "text": f"Instruction: {instruction}"}]
    body = {"model": STATE["bonsai_model"] or "default", "messages": [
        {"role": "system", "content": REWRITE}, {"role": "user", "content": content}],
        "max_tokens": 600, "temperature": 0.4, "enable_thinking": False, "stream": False}
    try:
        r = _post(STATE["bonsai"].rstrip("/") + "/v1/chat/completions", body, 300)
        text = ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip().strip('"')
        if len(text) > 20:
            return text, "bonsai"
    except Exception as e:  # Bonsai 没开 / 超时：照样出图
        sys.stderr.write(f"[fal-shim] Bonsai 改写失败，用原指令：{e}\n")
    return instruction, "instruction"


def run_image(endpoint: str, inp: dict) -> dict:
    prompt = str(inp.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    out_dir = Path(STATE["out"]).resolve()      # 绝对路径：Flux 服务的工作目录不同
    out_dir.mkdir(parents=True, exist_ok=True)
    srcs = input_images(inp)
    init_path, raw, mime = None, None, "image/png"
    if srcs:
        raw, mime = _fetch_bytes(srcs[0])
        ext = ".jpg" if "jpeg" in mime or "jpg" in mime else ".png"
        init_path = out_dir / f"in-{uuid.uuid4().hex[:10]}{ext}"
        init_path.write_bytes(raw)
    flux_prompt, source = rewrite_prompt(prompt, raw, mime) if srcs else (prompt, "prompt")
    w, h = _size(inp, init_path)
    n = max(1, min(int(inp.get("num_images", 1) or 1), 4))
    seed0 = int(inp["seed"]) if str(inp.get("seed", "")).lstrip("-").isdigit() else int(time.time()) % 100000
    images = []
    for i in range(n):
        name = f"out-{uuid.uuid4().hex[:10]}.png"
        req = {"prompt": flux_prompt, "out": str(out_dir / name), "width": w, "height": h, "seed": seed0 + i}
        if init_path is not None:
            req.update(init_image=str(init_path), strength=float(inp.get("strength", STATE["strength"])))
        r = _post(STATE["flux"].rstrip("/") + "/generate", req, 1800)
        if r.get("error"):
            raise RuntimeError("Flux: " + str(r["error"]))
        images.append({"url": f"{STATE['base']}/files/{name}", "content_type": "image/png", "file_name": name,
                       "width": w, "height": h})
    return {"images": images, "seed": seed0, "description": "",
            "local": {"backend": "flux", "prompt_source": source, "flux_prompt": flux_prompt, "endpoint": endpoint}}


def execute(endpoint: str, inp: dict) -> dict:
    k = kind_of(endpoint)
    if k == "image":
        return run_image(endpoint, inp)
    if k in NO_LOCAL:
        raise NotImplementedError(f"本机没有{k}模型，{endpoint} 不能本地替代；这一步需要真实的 fal.ai 服务。")
    raise NotImplementedError(f"不认识的端点 {endpoint}：本地服务只支持图像编辑 / 文生图。")


def _worker(rid: str) -> None:
    job = JOBS[rid]
    job["status"] = "IN_PROGRESS"
    try:
        job["result"] = execute(job["endpoint"], job["input"])
    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
    job["status"] = "COMPLETED"
    job["done_at"] = time.time()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].strip("/")
        if path == "health":
            return self._send(200, {"ok": True, "jobs": len(JOBS), "flux": STATE["flux"], "bonsai": STATE["bonsai"]})
        if path.startswith("files/"):
            f = Path(STATE["out"]) / Path(path[6:]).name
            if not f.is_file():
                return self._send(404, {"detail": "file not found"})
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if f.suffix == ".png" else "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        m = re.match(r"^(.+)/requests/([0-9a-f-]+)(/status)?$", path)
        if not m or m.group(2) not in JOBS:
            return self._send(404, {"detail": "request not found"})
        job = JOBS[m.group(2)]
        if m.group(3):
            st = {"status": job["status"], "request_id": m.group(2), "logs": []}
            if job.get("error"):
                st["error"] = job["error"]
            return self._send(200, st)
        if job["status"] != "COMPLETED":
            return self._send(400, {"detail": "request is still in progress"})
        if job.get("error"):
            return self._send(500, {"detail": job["error"]})
        return self._send(200, job["result"])

    def do_POST(self):
        path = self.path.split("?", 1)[0].strip("/")
        try:
            n = int(self.headers.get("Content-Length", 0))
            inp = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"detail": "body must be JSON"})
        if path.startswith("run/"):                          # 同步：对应 https://fal.run/<endpoint>
            try:
                return self._send(200, execute(path[4:], inp))
            except NotImplementedError as e:
                return self._send(501, {"detail": str(e)})
            except Exception as e:
                return self._send(500, {"detail": f"{type(e).__name__}: {e}"})
        k = kind_of(path)
        if k != "image":                                     # 提交时就拒绝，调用方马上看到原因
            msg = (f"本机没有{k}模型，{path} 不能本地替代；这一步需要真实的 fal.ai 服务。" if k in NO_LOCAL
                   else f"不认识的端点 {path}：本地服务只支持图像编辑 / 文生图。")
            return self._send(501, {"detail": msg})
        rid = str(uuid.uuid4())
        with _lock:
            JOBS[rid] = {"endpoint": path, "input": inp, "status": "IN_QUEUE", "at": time.time()}
        threading.Thread(target=_worker, args=(rid,), daemon=True).start()
        base = f"{STATE['base']}/{path}/requests/{rid}"
        return self._send(200, {"request_id": rid, "status_url": base + "/status", "response_url": base,
                                "cancel_url": base + "/cancel"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[fal-shim] " + fmt % args + "\n")


def configure(port: int, config_path: str | None = None) -> None:
    STATE["base"] = f"http://127.0.0.1:{port}"
    p = Path(config_path) if config_path else ROOT / "config" / "pipeline.toml"
    if p.is_file() and tomllib is not None:
        with open(p, "rb") as fh:
            CFG.update(tomllib.load(fh))
    img = CFG.get("image", {})
    STATE["flux"] = f"http://127.0.0.1:{img.get('port', 8090)}"
    main = CFG.get("llm", {}).get("main", {})
    if main:
        STATE["bonsai"] = f"http://127.0.0.1:{main.get('port', 8081)}"
        mp = Path(main.get("model_path", ""))
        STATE["bonsai_model"] = str(mp if mp.is_absolute() else ROOT / mp)
    out = CFG.get("pipeline", {}).get("runs_dir")
    if out:
        STATE["out"] = (Path(out) if Path(out).is_absolute() else ROOT / out) / "_fal_shim"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-bonsai", action="store_true", help="不用 Bonsai 改写编辑指令，直接把指令交给 Flux")
    ap.add_argument("--strength", type=float, default=0.6, help="图生图重绘强度（0~1，越大改得越多）")
    a = ap.parse_args()
    configure(a.port, a.config)
    STATE["strength"] = a.strength
    if a.no_bonsai:
        STATE["bonsai"] = ""
    print(f"fal-shim listening on {STATE['base']}  (Flux {STATE['flux']}, Bonsai {STATE['bonsai'] or '关闭'})",
          flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
