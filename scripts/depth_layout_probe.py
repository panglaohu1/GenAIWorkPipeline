#!/usr/bin/env python3
"""验证：目标图 → SAM 3 实例掩码 + 度量深度/相机 → 每个物件在场景里的位置、占地、高度，以及海面 / 地面平面。

用法（envs/depth 的 python）：
  envs/depth/bin/python scripts/depth_layout_probe.py IMAGE OUT_DIR [--prompts "lighthouse,windmill,..."] [--model moge2|da3]
输出：OUT_DIR/layout.json（米制布局）、OUT_DIR/layout_probe.png（掩码 + 俯视图 + 侧视图）。
坐标：以海面为 z=0 的世界系，z 朝上（海面法线），y 为相机视线在海面上的投影方向（远离相机为正），x 向右。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def sam_masks(image: str, prompts: list[str], out_dir: Path) -> dict:
    code = ("import sys, json; sys.path.insert(0, 'src/genai_pipeline'); import sam_server as S; "
            f"S.WEIGHTS = {str(ROOT / 'models/vision/sam3-image/model.safetensors')!r}; "
            f"print('@@' + json.dumps(S.segment({image!r}, {prompts!r}, {str(out_dir)!r}, threshold=0.3)))")
    r = subprocess.run([str(ROOT / "envs/sam3/bin/python"), "-c", code], cwd=ROOT, capture_output=True, text=True)
    line = next((l for l in r.stdout.splitlines() if l.startswith("@@")), None)
    if line is None:
        raise RuntimeError("SAM 失败：" + r.stderr[-2000:])
    return json.loads(line[2:])


def fit_plane(pts: np.ndarray, iters: int = 400, tol: float = 0.05, seed: int = 0):
    """RANSAC 平面：返回 (单位法线 n, d)，n·p + d = 0；tol 按点云尺度自适应。"""
    rng = np.random.default_rng(seed)
    if len(pts) > 50000:   # 平面拟合用 5 万个点足够
        pts = pts[rng.choice(len(pts), 50000, replace=False)]
    scale = np.median(np.linalg.norm(pts - np.median(pts, 0), axis=1)) + 1e-6
    best, best_in = None, -1
    for _ in range(iters):
        a, b, c = pts[rng.choice(len(pts), 3, replace=False)]
        n = np.cross(b - a, c - a)
        if np.linalg.norm(n) < 1e-9:
            continue
        n /= np.linalg.norm(n)
        d = -n @ a
        inl = np.abs(pts @ n + d) < tol * scale
        if inl.sum() > best_in:
            best, best_in = (n, d), inl.sum()
    n, d = best
    inl = np.abs(pts @ n + d) < tol * scale
    q = pts[inl]
    c = q.mean(0)
    # 只要 3×3 协方差的最小特征向量。千万不能对 N×3 点阵直接 np.linalg.svd(默认 full_matrices=True)：
    # 那会生成 N×N 的 U 矩阵（30 万个点 ≈ 700 GB），2026-10-24 曾把整机内存吃光。
    w, v = np.linalg.eigh(np.cov((q - c).T))
    n = v[:, 0] / np.linalg.norm(v[:, 0])
    return n, -n @ c, float(inl.mean())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("out")
    ap.add_argument("--prompts", default="lighthouse,windmill,house,wooden pier,boat,island,sea,tree")
    ap.add_argument("--model", default="moge2")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    prompts = [p.strip() for p in a.prompts.split(",") if p.strip()]
    seg = sam_masks(a.image, prompts, out / "masks")
    npz = out / f"depth_{a.model}.npz"
    subprocess.run([sys.executable, str(ROOT / "src/genai_pipeline/depth_estimate.py"), a.image, str(npz), "--model", a.model],
                   check=True, cwd=ROOT)
    D = np.load(npz)
    step = max(1, D["points"].shape[1] // 512)   # 分析用 ≤512 px 的点云：几何统计足够，内存降到 1/4 以下
    P = D["points"][::step, ::step].astype(np.float32)
    valid = D["mask"][::step, ::step].astype(bool) & np.isfinite(P).all(-1)
    K = D["intrinsics"]
    del D
    H, W = P.shape[:2]

    def mask_of(path):
        m = np.asarray(Image.open(path).convert("L").resize((W, H), Image.NEAREST)) > 127
        return m & valid

    inst = []
    for p, items in seg["results"].items():
        for k, it in enumerate(items):
            inst.append({"prompt": p, "k": k, "score": it["score"], "box": it["box"], "mask": mask_of(it["mask"])})
    sea = [i for i in inst if i["prompt"] in ("sea", "water", "ocean")]
    if sea:
        sm = np.logical_or.reduce([i["mask"] for i in sea])
        n, d, frac = fit_plane(P[sm])
        plane_src = "sea"
    else:   # 没有海：用最低的 30% 点拟合地面
        pts = P[valid]
        n, d, frac = fit_plane(pts[pts[:, 1] > np.percentile(pts[:, 1], 70)])
        plane_src = "lowest-30%"
    if n[1] > 0:      # 让法线朝上（OpenCV 相机 y 朝下）
        n, d = -n, -d
    # 世界系：z = 海面法线；y = 视线方向（相机 z 轴）在海面上的投影；x = y × z
    fwd = np.array([0, 0, 1.0]) - n * n[2]
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, n)
    origin = -d * n   # 海面上离相机最近的点

    def world(pts):
        q = pts - origin
        return np.stack([q @ right, q @ fwd, q @ n], -1)

    cam = world(np.zeros((1, 3)))[0]
    fov_x = float(np.degrees(2 * np.arctan(0.5 / K[0, 0])))
    pitch = float(np.degrees(np.arcsin(-n[2])))   # 视线与海面的夹角（向下为正）
    objs = []
    for i in inst:
        if i["prompt"] in ("sea", "water", "ocean") or i["mask"].sum() < 30:
            continue
        q = world(P[i["mask"]])
        lo, hi = np.percentile(q, 5, 0), np.percentile(q, 95, 0)
        objs.append({"name": f"{i['prompt']}#{i['k']}", "score": i["score"], "pixels": int(i["mask"].sum()),
                     "center_xy_m": [round(float(np.median(q[:, 0])), 2), round(float(np.median(q[:, 1])), 2)],
                     "base_z_m": round(float(lo[2]), 2), "top_z_m": round(float(hi[2]), 2),
                     "height_m": round(float(hi[2] - lo[2]), 2),
                     "footprint_xy_m": [round(float(hi[0] - lo[0]), 2), round(float(hi[1] - lo[1]), 2)],
                     "visible_depth_note": "单视角只看到朝向相机的表面：前后进深偏小"})
    isl = [o for o in objs if o["name"].startswith("island")]
    ref = isl[0]["center_xy_m"] if isl else [0, 0]
    for o in objs:   # 相对岛中心的位置，方便和拆分方案对照
        o["rel_island_xy_m"] = [round(o["center_xy_m"][0] - ref[0], 2), round(o["center_xy_m"][1] - ref[1], 2)]
    res = {"image": a.image, "model": a.model, "plane": plane_src, "plane_inlier_frac": round(frac, 3),
           "camera": {"height_above_sea_m": round(float(cam[2]), 2), "pitch_down_deg": round(pitch, 1),
                      "fov_x_deg": round(fov_x, 1), "distance_to_island_m": round(float(np.hypot(*(np.array(ref) - cam[:2]))), 2)},
           "objects": sorted(objs, key=lambda o: -o["pixels"])}
    (out / "layout.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    # 可视化
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for fp in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc"):
        if Path(fp).exists():
            font_manager.fontManager.addfont(fp)
            plt.rcParams["font.family"] = font_manager.FontProperties(fname=fp).get_name()
            break
    img = np.asarray(Image.open(a.image).convert("RGB").resize((W, H)))
    fig, ax = plt.subplots(1, 3, figsize=(21, 7), dpi=100)
    ax[0].imshow(img)
    cmap = plt.get_cmap("tab10")
    ov = np.zeros((H, W, 4), np.uint8)
    for j, i in enumerate(inst):
        c = np.array(cmap(j % 10)[:3])
        ov[i["mask"]] = [*(c * 255).astype(np.uint8), 115]
    ax[0].imshow(ov)
    for j, i in enumerate(inst):
        c = np.array(cmap(j % 10)[:3])
        ys, xs = np.nonzero(i["mask"])
        if len(xs):
            ax[0].text(xs.mean(), ys.mean(), f"{i['prompt']}#{i['k']}", color="white", fontsize=9,
                       bbox=dict(facecolor=c, alpha=0.8, pad=1))
    ax[0].set_title("SAM 3 实例"); ax[0].axis("off")
    wp = world(P[valid][::7])
    for axi, (u, v, t) in zip(ax[1:], ((0, 1, "俯视（米）：x 右，y 远离相机"), (1, 2, "侧视（米）：y 远离相机，z 高出海面"))):
        axi.scatter(wp[:, u], wp[:, v], s=0.3, c="#bbbbbb")
        for j, i in enumerate(inst):
            if i["prompt"] in ("sea",):
                continue
            q = world(P[i["mask"]][::5])
            axi.scatter(q[:, u], q[:, v], s=0.6, color=cmap(j % 10))
        for o in objs:
            pt = o["center_xy_m"] if u == 0 else [o["center_xy_m"][1], o["top_z_m"]]
            axi.annotate(f"{o['name']} h{o['height_m']}m", pt, fontsize=8)
        if v == 2:
            axi.axhline(0, color="#3a7bd5", lw=1)
            axi.scatter([cam[1]], [cam[2]], marker="^", s=80, c="k")
            axi.annotate(f"相机 {cam[2]:.1f}m", (cam[1], cam[2]))
        axi.set_title(t); axi.set_aspect("equal"); axi.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(out / "layout_probe.png")
    plt.close(fig)
    print(json.dumps(res, ensure_ascii=False, indent=1)[:4000])


if __name__ == "__main__":
    main()
