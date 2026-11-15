"""实例分割服务（在 envs/sam3 中运行）：SAM 3（MLX 版）按文字提示找出场景图里的全部实例。

  POST /segment {"image": 路径, "prompts": ["crystal tower", ...], "out_dir": 目录, "threshold": 0.4}
       -> {"results": {prompt: [{"box": [x0,y0,x1,y1] 归一化, "score", "area": 面积占比, "mask": png 路径}]},
           "size": [w, h], "elapsed_ms"}
  GET  /health
掩码按置信度排序，每个提示最多保留 max_instances 个；与更高分实例重叠（IoU>0.7）的重复检测会被去掉。
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

_proc = None
_lock = threading.Lock()
WEIGHTS = ""


def processor():
    global _proc
    if _proc is None:
        from sam3 import build_sam3_image_model
        from sam3.model.sam3_image_processor import Sam3Processor
        model = build_sam3_image_model(checkpoint_path=WEIGHTS)
        _proc = Sam3Processor(model, confidence_threshold=0.3)
    return _proc


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    return float(inter) / float(max(1, np.logical_or(a, b).sum()))


def _main_part(m: np.ndarray) -> np.ndarray:
    """只保留掩码里的主体：最大连通块 + 面积不小于它 10% 的块（去掉远处零星碎点，免得撑大裁剪框）。"""
    from scipy import ndimage
    lab, n = ndimage.label(m)
    if n <= 1:
        return m
    sizes = ndimage.sum(m, lab, range(1, n + 1))
    keep = [i + 1 for i, v in enumerate(sizes) if v >= 0.1 * sizes.max()]
    return np.isin(lab, keep)


def segment(image: str, prompts: list[str], out_dir: str, threshold: float = 0.4,
            max_instances: int = 30) -> dict:
    img = Image.open(image).convert("RGB")
    w, h = img.size
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[dict]] = {}
    with _lock:
        p = processor()
        state = p.set_image(img)
        for pi, prompt in enumerate(prompts):
            st = p.set_text_prompt(prompt, dict(state))
            scores = np.array(st["scores"]).reshape(-1)
            masks = np.array(st["masks"]).reshape(len(scores), h, w) if len(scores) else np.zeros((0, h, w), bool)
            boxes = np.array(st["boxes"]).reshape(-1, 4)
            kept: list[int] = []
            for i in np.argsort(-scores):
                if scores[i] < threshold or len(kept) >= max_instances:
                    break
                if masks[i].sum() < 16 or any(_iou(masks[i], masks[j]) > 0.7 for j in kept):
                    continue
                kept.append(int(i))
            items = []
            for k, i in enumerate(kept):
                mp = out / f"p{pi:02d}_{k:02d}.png"
                Image.fromarray((_main_part(masks[i]) * 255).astype(np.uint8)).save(mp)
                x0, y0, x1, y1 = boxes[i].tolist()
                items.append({"box": [round(max(0, x0) / w, 4), round(max(0, y0) / h, 4),
                                      round(min(w, x1) / w, 4), round(min(h, y1) / h, 4)],
                              "score": round(float(scores[i]), 4), "area": round(float(masks[i].mean()), 5),
                              "mask": str(mp)})
            results[prompt] = items
    return {"results": results, "size": [w, h]}


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
            self._send(200, {"ok": True, "loaded": _proc is not None})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/segment":
            return self._send(404, {"error": "not found"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            t = time.time()
            res = segment(req["image"], req["prompts"], req["out_dir"], float(req.get("threshold", 0.4)),
                          int(req.get("max_instances", 30)))
            res["elapsed_ms"] = round((time.time() - t) * 1000)
            self._send(200, res)
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[sam3] " + fmt % args + "\n")


def main() -> None:
    global WEIGHTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8097)
    ap.add_argument("--weights", required=True)
    a = ap.parse_args()
    WEIGHTS = a.weights
    processor()
    print(f"sam3 listening on 127.0.0.1:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
