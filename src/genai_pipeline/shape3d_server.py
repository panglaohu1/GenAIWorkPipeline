"""图生 3D 服务（在 envs/shape3d 中运行）：Hunyuan3D-2mini Turbo，单张物体图 → 形体网格 GLB（不含贴图）。

借鉴「参考图 → 拆资产 → Tripo 生成核心 3D 资产 → Blender 摆放精修」的流程，本地用开源模型代替 Tripo：
配色交给 Blender 侧按参考图投影取色并做 Oskar 式色块量化，所以这里只出形体。

  POST /generate {"image": 物体图路径（RGBA 透明背景最好；RGB 会按近灰背景抠图）, "out": 输出 .glb,
                  "seed": 0, "steps": 5, "octree": 256, "guidance": 5.0, "num_chunks": 20000, "max_faces": 20000}
       -> {"path", "vertices", "faces", "seconds", "device"}
  GET  /health
命令行自检：python shape3d_server.py --selftest 图片 --out 输出.glb
模型目录由环境变量 HY3DGEN_MODELS 指定（models/shape3d），离线加载，不联网。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

MODEL = "tencent/Hunyuan3D-2mini"
SUBFOLDER = "hunyuan3d-dit-v2-mini-turbo"
_pipe = None
_device = None
_lock = threading.Lock()


def pipeline():
    global _pipe, _device
    if _pipe is None:
        import torch
        from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
        _device = "mps" if torch.backends.mps.is_available() else "cpu"
        dtype = torch.float16 if _device == "mps" else torch.float32
        _pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(MODEL, subfolder=SUBFOLDER, use_safetensors=True,
                                                                 variant="fp16", device=_device, dtype=dtype)
        try:  # FlashVDM：turbo VAE + 自适应体素解码，大幅加快出网格；不可用时退回普通解码
            _pipe.enable_flashvdm(mc_algo="mc")
        except Exception as e:  # noqa: BLE001
            print(f"[shape3d] FlashVDM 不可用，使用普通解码：{type(e).__name__}: {e}", flush=True)
    return _pipe


def _prepare(image: str):
    """RGBA 直接用；RGB 图把与边框颜色相近的背景抠成透明（流水线的裁剪图/单物体图是灰底），保证模型只看到物体。
    边框取色失败（几乎整图都被当成前景，常见于画满背景的孤立图）时，改试浅灰底阈值，仍失败则原图加满不透明 alpha，避免空图进模型。"""
    import numpy as np
    from PIL import Image
    img = Image.open(image)
    if img.mode == "RGBA" and img.getextrema()[3][0] < 250:
        return img
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)

    def key(bg, thr: int) -> np.ndarray:
        return (np.abs(rgb - bg).sum(axis=2) > thr).astype(np.uint8) * 255

    border = np.concatenate([rgb[0, :], rgb[-1, :], rgb[:, 0], rgb[:, -1]])
    alpha = key(np.median(border, axis=0), 24)
    if alpha.mean() > 0.90 * 255:  # 边框取色没抠出背景：试浅灰（FLUX 单物体图常见）
        alpha = key(np.array([180, 180, 180], dtype=np.int16), 40)
    if alpha.mean() < 0.002 * 255 or alpha.mean() > 0.98 * 255:
        # 仍不可靠：不要送空 alpha（会触发后续空网格 IndexError），整图不透明交给模型
        alpha = np.full(rgb.shape[:2], 255, dtype=np.uint8)
    out = np.dstack([rgb.astype(np.uint8), alpha])
    return Image.fromarray(out, "RGBA")


def generate(image: str, out: str, seed: int = 0, steps: int = 5, octree: int = 256, guidance: float = 5.0,
             num_chunks: int = 20000, max_faces: int = 20000) -> dict:
    import torch
    from hy3dgen.shapegen import DegenerateFaceRemover, FaceReducer, FloaterRemover
    t = time.time()
    with _lock:
        p = pipeline()
        gen = torch.Generator(device="cpu").manual_seed(int(seed))
        mesh = p(image=_prepare(image), num_inference_steps=int(steps), octree_resolution=int(octree),
                 guidance_scale=float(guidance), num_chunks=int(num_chunks), generator=gen,
                 output_type="trimesh")[0]
    if mesh is None or len(getattr(mesh, "faces", [])) == 0:
        raise RuntimeError("没有生成出表面（输入图里可能没有清晰的单个物体）")
    for fix in (FloaterRemover(), DegenerateFaceRemover()):
        try:
            mesh = fix(mesh)
        except Exception as e:  # noqa: BLE001 — 空/退化网格上 min() 会抛 IndexError
            raise RuntimeError(f"网格清理失败（输入图可能没有清晰的单个物体）：{type(e).__name__}: {e}") from e
        if mesh is None or len(getattr(mesh, "faces", [])) == 0:
            raise RuntimeError("清理后没有剩余表面（输入图里可能没有清晰的单个物体）")
    if max_faces and len(mesh.faces) > max_faces:
        mesh = FaceReducer()(mesh, max_facenum=int(max_faces))
        if mesh is None or len(getattr(mesh, "faces", [])) == 0:
            raise RuntimeError("减面后没有剩余表面")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    mesh.export(out)
    return {"path": out, "vertices": int(len(mesh.vertices)), "faces": int(len(mesh.faces)),
            "seconds": round(time.time() - t, 1), "device": _device}


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
            self._send(200, {"ok": True, "loaded": _pipe is not None, "device": _device})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/generate":
            return self._send(404, {"error": "not found"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            keys = ("seed", "steps", "octree", "guidance", "num_chunks", "max_faces")
            self._send(200, generate(req["image"], req["out"], **{k: req[k] for k in keys if k in req}))
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[shape3d] " + fmt % args + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--selftest", help="自检：用这张图生成一次后退出")
    ap.add_argument("--out", default="shape3d_selftest.glb")
    a = ap.parse_args()
    if a.selftest:
        res = generate(a.selftest, a.out)
        print("[shape3d] 自检通过：" + json.dumps(res, ensure_ascii=False), flush=True)
        return
    pipeline()
    print(f"shape3d listening on 127.0.0.1:{a.port} ({_device})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
