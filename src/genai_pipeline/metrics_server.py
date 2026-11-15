"""客观评分服务（在 envs/vision 中运行）：渲染图与目标图的图像特征相似度。

  POST /similarity {"target": 路径, "renders": [路径...], "target_mask": 可选掩码路径}
       -> {"siglip": {"max", "mean", "per"}, "dino": {...}, "elapsed_ms"}
  GET  /health
SigLIP-2 偏语义/风格，DINOv2 偏结构/形状。渲染图按纯色背景自动裁到物体包围盒；
给了 target_mask 时，目标图只保留掩码内物体并贴到与渲染相同的灰底上（P1：SAM 实例掩码）。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image

MODEL_ROOT = Path(__file__).resolve().parents[2] / "models" / "vision"
RENDER_BG = (116, 120, 126)  # 与 Blender 渲染的灰色背景相近
_emb = None
_lock = threading.Lock()


def fg_crop(img: Image.Image, margin: float = 0.08) -> Image.Image:
    """纯色背景图：按与角点颜色的差异找到物体包围盒并裁出，消除取景大小差异。"""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    bg = np.median(np.stack([a[0, 0], a[0, -1], a[-1, 0], a[-1, -1]]), axis=0)
    m = np.abs(a - bg).sum(-1) > 30
    if m.sum() < 50:
        return img.convert("RGB")
    ys, xs = np.where(m)
    h, w = m.shape
    dx, dy = int((xs.max() - xs.min()) * margin) + 2, int((ys.max() - ys.min()) * margin) + 2
    return img.convert("RGB").crop((max(0, xs.min() - dx), max(0, ys.min() - dy),
                                    min(w, xs.max() + dx), min(h, ys.max() + dy)))


def masked_target(img: Image.Image, mask: Image.Image, margin: float = 0.08) -> Image.Image:
    """只保留掩码内的物体，贴到灰底并裁到掩码包围盒。"""
    img = img.convert("RGB")
    m = np.asarray(mask.convert("L").resize(img.size)) > 127
    if m.sum() < 50:
        return img
    bg = Image.new("RGB", img.size, RENDER_BG)
    bg.paste(img, (0, 0), Image.fromarray((m * 255).astype(np.uint8)))
    ys, xs = np.where(m)
    dx, dy = int((xs.max() - xs.min()) * margin) + 2, int((ys.max() - ys.min()) * margin) + 2
    return bg.crop((max(0, xs.min() - dx), max(0, ys.min() - dy),
                    min(img.width, xs.max() + dx), min(img.height, ys.max() + dy)))


class Embedder:
    def __init__(self) -> None:
        import torch
        from transformers import AutoImageProcessor, AutoModel
        self.torch = torch
        self.dev = "mps" if torch.backends.mps.is_available() else "cpu"
        load = lambda n: (AutoImageProcessor.from_pretrained(MODEL_ROOT / n),
                          AutoModel.from_pretrained(MODEL_ROOT / n).to(self.dev).eval())
        from transformers import AutoProcessor
        self.sp = AutoProcessor.from_pretrained(MODEL_ROOT / "siglip2-base-patch16-224")  # 含分词器：图文匹配用
        self.sm = AutoModel.from_pretrained(MODEL_ROOT / "siglip2-base-patch16-224").to(self.dev).eval()
        self.dp, self.dm = load("dinov2-base")

    def __call__(self, imgs: list[Image.Image]) -> dict[str, np.ndarray]:
        torch = self.torch
        with torch.no_grad():
            s = self.sm.get_image_features(**self.sp(images=imgs, return_tensors="pt").to(self.dev))
            s = getattr(s, "pooler_output", s)
            d = self.dm(**self.dp(images=imgs, return_tensors="pt").to(self.dev)).last_hidden_state[:, 0]
        norm = lambda t: torch.nn.functional.normalize(t.float(), dim=-1).cpu().numpy()
        return {"siglip": norm(s), "dino": norm(d)}


def text_match(images: list[str], texts: list[str]) -> list[list[float]]:
    """SigLIP-2 图文匹配：返回 [图][文] 的匹配概率（sigmoid），用于核验 SAM 实例是否就是要找的物体。"""
    e = embedder()
    torch = e.torch
    imgs = [Image.open(p).convert("RGB") for p in images]
    with _lock, torch.no_grad():
        x = e.sp(images=imgs, text=texts, padding="max_length", max_length=64, return_tensors="pt").to(e.dev)
        out = e.sm(**x)
        probs = torch.sigmoid(out.logits_per_image.float()).cpu().numpy()
    return [[round(float(v), 4) for v in row] for row in probs]


def embedder() -> Embedder:
    global _emb
    if _emb is None:
        _emb = Embedder()
    return _emb


def similarity(target: str, renders: list[str], target_mask: str | None = None) -> dict:
    t = Image.open(target)
    t = masked_target(t, Image.open(target_mask)) if target_mask else t.convert("RGB")
    imgs = [t] + [fg_crop(Image.open(r)) for r in renders]
    with _lock:
        e = embedder()(imgs)
    out = {}
    for k, v in e.items():
        sims = v[1:] @ v[0]
        out[k] = {"max": round(float(sims.max()), 4), "mean": round(float(sims.mean()), 4),
                  "per": [round(float(x), 4) for x in sims]}
    return out


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
            self._send(200, {"ok": True, "loaded": _emb is not None})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/similarity", "/text_match"):
            return self._send(404, {"error": "not found"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            t = time.time()
            if self.path == "/text_match":
                res = {"probs": text_match(req["images"], req["texts"])}
            else:
                res = similarity(req["target"], req["renders"], req.get("target_mask"))
            res["elapsed_ms"] = round((time.time() - t) * 1000)
            self._send(200, res)
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[metrics] " + fmt % args + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8096)
    a = ap.parse_args()
    embedder()
    print(f"metrics listening on 127.0.0.1:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
