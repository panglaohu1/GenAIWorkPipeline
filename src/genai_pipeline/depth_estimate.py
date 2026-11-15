"""单张图 → 度量深度 + 相机（在 envs/depth 中运行）。

  python depth_estimate.py IMAGE OUT.npz [--model moge2|da3] [--device mps|cpu]
输出 npz：points (H,W,3) 相机坐标系下的米制点（OpenCV：x 右、y 下、z 前），depth (H,W) 米，
intrinsics (3,3) 归一化内参（fx、fy 以图像宽高为 1），mask (H,W) 有效像素，normal (H,W,3)（MoGe-2 normal 版才有）。
MoGe-2：MIT 许可，单图直接给米制点图与内参。Depth Anything 3 度量版：Apache-2.0，输出需按 focal 换算成米。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import torch
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def pick_device(name: str) -> torch.device:
    if name == "mps" and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def run_moge2(img: np.ndarray, device: torch.device) -> dict:
    sys.path.insert(0, str(ROOT / "vendor" / "MoGe"))
    from moge.model.v2 import MoGeModel
    model = MoGeModel.from_pretrained(str(ROOT / "models" / "vision" / "moge-2-vitl-normal" / "model.pt")).to(device).eval()
    x = torch.tensor(img / 255.0, dtype=torch.float32, device=device).permute(2, 0, 1)
    with torch.no_grad():
        out = model.infer(x, use_fp16=False)   # MPS 不支持半精度卷积偏置混用
    res = {k: v.detach().float().cpu().numpy() for k, v in out.items() if isinstance(v, torch.Tensor)}
    return res


def run_da3(img: np.ndarray, device: torch.device, focal_px: float | None) -> dict:
    sys.path.insert(0, str(ROOT / "vendor" / "Depth-Anything-3" / "src"))
    from depth_anything_3.api import DepthAnything3
    model = DepthAnything3.from_pretrained(str(ROOT / "models" / "vision" / "da3metric-large")).to(device)
    pred = model.inference([Image.fromarray(img)])
    raw = np.asarray(pred.depth[0], dtype=np.float32)
    h, w = raw.shape
    if focal_px is None:
        focal_px = 0.5 * w / np.tan(np.radians(30))   # 没有内参时按 60° 水平视角估计
    else:
        focal_px = focal_px * w / img.shape[1]
    depth = focal_px * raw / 300.0   # 官方换算：metric_depth = focal * net_output / 300
    fx = focal_px / w
    K = np.array([[fx, 0, 0.5], [0, fx * w / h, 0.5], [0, 0, 1]], dtype=np.float32)
    v, u = np.mgrid[0:h, 0:w]
    xs = (u + 0.5 - 0.5 * w) / focal_px * depth
    ys = (v + 0.5 - 0.5 * h) / focal_px * depth
    return {"depth": depth, "points": np.stack([xs, ys, depth], -1), "intrinsics": K, "mask": depth > 0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("out")
    ap.add_argument("--model", default="moge2", choices=["moge2", "da3"])
    ap.add_argument("--device", default="mps")
    ap.add_argument("--focal-px", type=float, default=None, help="DA3 用：已知焦距（像素，按原图宽）")
    a = ap.parse_args()
    img = np.asarray(Image.open(a.image).convert("RGB"))
    dev = pick_device(a.device)
    t0 = time.time()
    try:
        res = run_moge2(img, dev) if a.model == "moge2" else run_da3(img, dev, a.focal_px)
    except Exception as e:  # MPS 不支持的算子：退回 CPU 再试一次
        if dev.type != "mps":
            raise
        print(f"[depth] MPS 失败（{type(e).__name__}: {e}），改用 CPU", flush=True)
        dev = torch.device("cpu")
        res = run_moge2(img, dev) if a.model == "moge2" else run_da3(img, dev, a.focal_px)
    np.savez_compressed(a.out, **res)
    print(f"[depth] {a.model} on {dev}: {time.time() - t0:.1f}s, keys={sorted(res)}, "
          f"depth {np.nanmin(res['depth']):.2f}–{np.nanmax(res['depth']):.2f} m", flush=True)


if __name__ == "__main__":
    main()
