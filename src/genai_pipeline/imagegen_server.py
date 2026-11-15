"""生图服务（在 envs/image 中运行，只依赖标准库 + mflux + pillow）。

后端:
  flux        — FLUX.1-schnell mflux 4-bit，常驻内存，空闲超时后卸载
  qwen_image  — Qwen-Image 2.1 GGUF，经 stable-diffusion.cpp 的 sd-cli 子进程运行（用完即释放内存）

接口:
  GET  /health                 -> {"ok": true, "loaded": [...], "busy": bool}
  POST /generate  JSON         -> {"path", "seconds", "backend", "seed"}
       {prompt, out, backend?, width?, height?, steps?, seed?, init_image?, strength?, negative_prompt?}
  POST /unload                 -> 卸载常驻模型
"""
from __future__ import annotations

import argparse
import gc
import json
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CFG: dict = {}
_gen_lock = threading.Lock()
_flux = None
_last_used = time.time()
_busy = False


def res(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else ROOT / q


def load_flux():
    global _flux
    if _flux is None:
        from mflux.models.common.config.model_config import ModelConfig
        from mflux.models.flux.variants.txt2img.flux import Flux1

        fc = CFG["image"]["flux"]
        _flux = Flux1(model_config=ModelConfig.from_name(model_name=fc.get("base_model", "schnell"), base_model=None),
                      model_path=str(res(fc["model_path"])), quantize=None)
    return _flux


def unload() -> None:
    global _flux
    _flux = None
    gc.collect()
    try:
        import mlx.core as mx
        mx.clear_cache()
    except Exception:
        pass


def gen_flux(req: dict) -> dict:
    fc = CFG["image"]["flux"]
    model = load_flux()
    seed = int(req.get("seed", int(time.time()) % 100000))
    kw = dict(seed=seed, prompt=req["prompt"], num_inference_steps=int(req.get("steps", fc.get("steps", 4))),
              width=int(req.get("width", fc.get("width", 1024))), height=int(req.get("height", fc.get("height", 1024))))
    if req.get("init_image"):
        kw.update(image_path=req["init_image"], image_strength=float(req.get("strength", 0.6)))
    img = model.generate_image(**kw)
    img.image.save(req["out"])
    return {"seed": seed}


def gen_qwen_image(req: dict) -> dict:
    qc = CFG["image"]["qwen_image"]
    seed = int(req.get("seed", int(time.time()) % 100000))
    cmd = [str(res(qc["sd_cli"])), "--diffusion-model", str(res(qc["diffusion_model"])),
           "--vae", str(res(qc["vae"])), "--llm", str(res(qc["llm"])),
           "-p", req["prompt"], "--cfg-scale", str(qc.get("cfg_scale", 6.0)), "--sampling-method", "euler",
           "--steps", str(req.get("steps", qc.get("steps", 30))),
           "-W", str(req.get("width", qc.get("width", 1024))), "-H", str(req.get("height", qc.get("height", 1024))),
           "-s", str(seed), "--fa", "-o", req["out"]]
    if req.get("negative_prompt"):
        cmd += ["-n", req["negative_prompt"]]
    if req.get("init_image"):
        mmproj = res(qc["llm"]).with_name("mmproj-Qwen3VL-8B-Instruct-F16.gguf")
        cmd += ["-r", req["init_image"], "--llm_vision", str(mmproj)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not Path(req["out"]).exists():
        raise RuntimeError("sd-cli failed: " + (r.stderr or r.stdout)[-2000:])
    return {"seed": seed}


def generate(req: dict) -> dict:
    global _last_used, _busy
    backend = req.get("backend") or CFG["image"].get("default_backend", "flux")
    Path(req["out"]).parent.mkdir(parents=True, exist_ok=True)
    with _gen_lock:
        _busy = True
        t = time.time()
        try:
            if backend == "flux":
                extra = gen_flux(req)
            elif backend == "qwen_image":
                unload()  # 给 Qwen-Image 腾出内存
                extra = gen_qwen_image(req)
            else:
                raise ValueError(f"unknown backend {backend}")
        finally:
            _busy = False
            _last_used = time.time()
    return {"path": req["out"], "seconds": round(time.time() - t, 1), "backend": backend, **extra}


def idle_reaper() -> None:
    limit = CFG["image"].get("idle_unload_s", 600)
    while True:
        time.sleep(30)
        if _flux is not None and not _busy and time.time() - _last_used > limit:
            with _gen_lock:
                unload()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "loaded": ["flux"] if _flux is not None else [], "busy": _busy})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/generate":
                if not req.get("prompt") or not req.get("out"):
                    return self._send(400, {"error": "prompt and out are required"})
                self._send(200, generate(req))
            elif self.path == "/unload":
                with _gen_lock:
                    unload()
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # 返回错误给调用方，服务保持运行
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[imagegen] " + fmt % args + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--config", default=str(ROOT / "config" / "pipeline.toml"))
    a = ap.parse_args()
    with open(a.config, "rb") as fh:
        CFG.update(tomllib.load(fh))
    threading.Thread(target=idle_reaper, daemon=True).start()
    print(f"imagegen listening on 127.0.0.1:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
