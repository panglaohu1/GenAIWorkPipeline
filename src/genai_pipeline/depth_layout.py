"""深度定位：目标图 → 本机度量深度（MoGe-2，envs/depth）+ SAM 3 实例掩码 → 场景的支撑平面、相机、每个实例的米制位置与尺寸。

管线只负责组织：尺寸、位置、相机、水面/地面高度全部由本地模型测出来，不靠 LLM 估，也不靠关键词规则。
世界系：支撑平面（海面 / 地面）为 z=0、z 朝上；y = 相机视线在平面上的投影（远离相机为正）；x 向右。
原点在 measure() 里是相机正下方平面上的点；apply 时再平移到地形中心。

内存：分析只用 ≤512 px 的点云；平面拟合最多 5 万点，法线只解 3×3 协方差（不对 N×3 做 SVD）。
深度模型在子进程里跑完即退出，不常驻。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
MAX_SIDE = 512


def _depth_cmd(image: str, out_npz: Path, model: str, focal_px: float | None, timeout: int) -> None:
    py = ROOT / "envs" / "depth" / "bin" / "python"
    env = {**os.environ, "KMP_DUPLICATE_LIB_OK": "TRUE", "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    cmd = [str(py), str(ROOT / "src" / "genai_pipeline" / "depth_estimate.py"), image, str(out_npz), "--model", model]
    if focal_px:
        cmd += ["--focal-px", f"{focal_px:.3f}"]
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not out_npz.exists():
        raise RuntimeError(f"深度估计（{model}）失败：" + (r.stderr or r.stdout)[-1500:])


def _fuse_da3(moge_npz: Path, da3_npz: Path, out_npz: Path) -> dict:
    """DA3 的深度形状 + MoGe-2 的内参、有效区（去天空）和米制尺度。
    DA3 度量版按官方公式换算出的绝对尺度在这类风格化图上不稳定（实测比 MoGe-2 小 4 倍多），
    这里用两者在共同有效像素上的深度中位比把 DA3 校到 MoGe-2 的米制，再用 MoGe-2 内参反投影成点。"""
    M = np.load(moge_npz)
    A = np.load(da3_npz)
    mz, mk, K = M["depth"].astype(np.float32), M["mask"].astype(bool), np.array(M["intrinsics"], dtype=np.float64)
    H, W = mz.shape
    az = A["depth"].astype(np.float32)
    ys = (np.arange(H) * az.shape[0] // H).clip(0, az.shape[0] - 1)
    xs = (np.arange(W) * az.shape[1] // W).clip(0, az.shape[1] - 1)
    az = az[ys][:, xs]
    ok = mk & np.isfinite(mz) & np.isfinite(az) & (az > 1e-6) & (mz > 1e-6)
    if ok.sum() < 100:
        raise RuntimeError("DA3 与 MoGe-2 没有足够的共同有效像素")
    scale = float(np.median(mz[ok] / az[ok]))
    depth = az * scale
    fx, fy, cx, cy = K[0, 0] * W, K[1, 1] * H, K[0, 2] * W, K[1, 2] * H
    v, u = np.mgrid[0:H, 0:W]
    pts = np.stack([(u + 0.5 - cx) / fx * depth, (v + 0.5 - cy) / fy * depth, depth], -1).astype(np.float32)
    mask = ok
    extra = {"normal": M["normal"]} if "normal" in M.files else {}
    np.savez_compressed(out_npz, points=pts, depth=depth, mask=mask, intrinsics=K.astype(np.float32), **extra)
    agree = float(np.median(np.abs(depth[ok] - mz[ok]) / mz[ok]))
    return {"da3_scale_to_moge": round(scale, 4), "median_rel_diff_vs_moge": round(agree, 4)}


def run_depth(image: str, out_dir: Path, model: str = "da3", timeout: int = 900) -> tuple[Path, dict]:
    """返回 (用于测量的 npz, 说明)。da3：Depth Anything 3 度量版出深度形状，内参、去天空的有效区与米制尺度取 MoGe-2；
    moge2：直接用 MoGe-2 的米制点图。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    moge = out_dir / "depth_moge2.npz"
    _depth_cmd(image, moge, "moge2", None, timeout)
    if model == "moge2":
        return moge, {"depth": "moge2", "intrinsics": "moge2"}
    with Image.open(image) as im:
        width = im.width
    fx = float(np.load(moge)["intrinsics"][0, 0]) * width
    da3 = out_dir / "depth_da3.npz"
    _depth_cmd(image, da3, "da3", fx, timeout)
    fused = out_dir / "depth_da3_fused.npz"
    info = _fuse_da3(moge, da3, fused)
    return fused, {"depth": "da3", "intrinsics": "moge2", "scale": "moge2", "focal_px": round(fx, 1), **info}


def fit_plane(pts: np.ndarray, iters: int = 400, tol: float = 0.03, seed: int = 0):
    """RANSAC 平面 → (单位法线 n, d, 内点比例)，n·p + d = 0。"""
    rng = np.random.default_rng(seed)
    if len(pts) > 50000:
        pts = pts[rng.choice(len(pts), 50000, replace=False)]
    scale = float(np.median(np.linalg.norm(pts - np.median(pts, 0), axis=1))) + 1e-6
    best, best_in = None, -1
    for _ in range(iters):
        a, b, c = pts[rng.choice(len(pts), 3, replace=False)]
        n = np.cross(b - a, c - a)
        ln = np.linalg.norm(n)
        if ln < 1e-9:
            continue
        n = n / ln
        cnt = int((np.abs(pts @ n - n @ a) < tol * scale).sum())
        if cnt > best_in:
            best, best_in = (n, -n @ a), cnt
    n, d = best
    inl = np.abs(pts @ n + d) < tol * scale
    q = pts[inl]
    c = q.mean(0)
    w, v = np.linalg.eigh(np.cov((q - c).T))   # 3×3：只取最小特征向量
    n = v[:, 0] / np.linalg.norm(v[:, 0])
    return n, float(-n @ c), float(inl.mean())


def _load(npz: Path):
    D = np.load(npz)
    step = max(1, max(D["points"].shape[:2]) // MAX_SIDE)
    P = D["points"][::step, ::step].astype(np.float32)
    valid = D["mask"][::step, ::step].astype(bool) & np.isfinite(P).all(-1)
    K = np.array(D["intrinsics"], dtype=np.float64)
    N = D["normal"][::step, ::step].astype(np.float32) if "normal" in D.files else None
    return P, valid, K, N


def plane_from_normals(P, N, free):
    """MoGe-2 法线图定支撑面：水平面（海面、地面、岛顶、台面）法线方向相同，取它们的共同朝上方向；
    支撑面 = 这些水平像素里最低的那一层（海面 / 地面），而不是被拍得更多的岛顶。返回 (n, d, 说明) 或 None。"""
    ok = free & np.isfinite(N).all(-1)
    nn = N[ok]
    if len(nn) < 400:
        return None
    nn = nn / (np.linalg.norm(nn, axis=1, keepdims=True) + 1e-9)
    up = nn[nn[:, 1] < -0.5]            # OpenCV 相机 y 朝下：朝上的面法线 y 分量为负
    if len(up) < 200:
        return None
    n = up.mean(0); n /= np.linalg.norm(n)
    for _ in range(3):
        sel = up[up @ n > np.cos(np.radians(20))]
        if len(sel) < 100:
            return None
        n = sel.mean(0); n /= np.linalg.norm(n)
    horiz = ok.copy()
    horiz[ok] = (nn @ n) > np.cos(np.radians(12))
    h = P[horiz] @ n                     # 沿朝上法线的「高度」（相机原点为 0，越往下越负）
    if len(h) < 200:
        return None
    hs = np.sort(h)
    low = hs[: max(50, int(0.3 * len(hs)))]   # 最低的那 30% 水平像素：海面 / 地面
    d = -float(np.median(low))
    return n.astype(np.float64), d, f"normals(horizontal px {int(horiz.sum())})"


def _mask(path: str, shape) -> np.ndarray:
    H, W = shape
    return np.asarray(Image.open(path).convert("L").resize((W, H), Image.NEAREST)) > 127


def _largest_blob(m: np.ndarray) -> np.ndarray:
    try:
        from scipy import ndimage
    except ImportError:
        return m
    lab, k = ndimage.label(m)
    if k <= 1:
        return m
    sizes = ndimage.sum(m, lab, range(1, k + 1))
    return lab == (int(np.argmax(sizes)) + 1)


def _erode(m: np.ndarray, r: int = 1) -> np.ndarray:
    """去掉掩码最外一圈：轮廓上的像素深度混着背景，会把物体前后拉出几十米。"""
    out = m.copy()
    for _ in range(r):
        e = out.copy()
        e[1:] &= out[:-1]; e[:-1] &= out[1:]; e[:, 1:] &= out[:, :-1]; e[:, :-1] &= out[:, 1:]
        out = e
    return out



def terrain_top_mask(t_mask: np.ndarray, cosu: np.ndarray, zmap: np.ndarray,
                     min_px: int = 30) -> np.ndarray:
    """岛顶草地掩码：斜视构图里真正水平的顶面像素很少（cosu>0.8 常只剩十几像素），
    按阈值从严到宽回退，避免 terrain_top 空掉导致草色 None、旧 png 残留。"""
    ztop = float(np.percentile(zmap[t_mask], 98)) if t_mask.any() else 0.0
    for c_thr, z_thr in ((0.8, 0.5), (0.65, 0.35), (0.55, 0.25), (0.5, 0.2)):
        m = _erode(t_mask & (cosu > c_thr) & (zmap > z_thr * ztop))
        if int(m.sum()) >= min_px:
            return m
    rock = _erode(t_mask & (np.abs(cosu) < 0.45))
    m = _erode(t_mask & ~rock & (zmap > 0.25 * ztop))
    if int(m.sum()) >= min_px:
        return m
    return _erode(t_mask & (cosu > 0.5) & (zmap > 0.15 * ztop))



def _label(m: np.ndarray) -> tuple[np.ndarray, int]:
    """4 邻接连通块标号（有 scipy 用 scipy，没有就用一遍 BFS；分析分辨率 ≤512 px，够快）。"""
    try:
        from scipy import ndimage
        lab, k = ndimage.label(m)
        return lab, int(k)
    except ImportError:
        pass
    from collections import deque
    H, W = m.shape
    lab = np.zeros((H, W), np.int32)
    k = 0
    for y0, x0 in zip(*np.nonzero(m)):
        if lab[y0, x0]:
            continue
        k += 1
        lab[y0, x0] = k
        q = deque([(y0, x0)])
        while q:
            y, x = q.popleft()
            for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= yy < H and 0 <= xx < W and m[yy, xx] and not lab[yy, xx]:
                    lab[yy, xx] = k
                    q.append((yy, xx))
    return lab, k


def terrain_shape(P, N, t_mask, world, up, right, fwd) -> dict:
    """地形的形体测量（给 Oskar 引擎当约束用）：
    - points：地形像素的世界坐标（抽样 ≤3000 点），apply 里做成俯视高度图（轮廓 + 每格高度）；
    - cliff：崖壁（法线接近水平的像素）的外倾角、单个平整面的宽度（按法线朝向分块的连通块）、面内粗糙度。"""
    q = world(P[t_mask])
    sel = np.linspace(0, len(q) - 1, min(len(q), 4000)).astype(int)
    out: dict = {"points": np.round(q[sel], 3).tolist()}
    if N is None:
        return out
    nn = N / (np.linalg.norm(N, axis=-1, keepdims=True) + 1e-9)
    fin = np.isfinite(nn).all(-1)
    cosu = np.nan_to_num(nn @ up.astype(np.float32))
    # 每个点是不是朝上的地表（岛顶、台地、滩）：俯视高度图只用地表点，崖面点的深度噪声会在崖前铺出一圈假「矮地」
    out["up"] = [int(c > 0.8) for c in cosu[t_mask][sel]]
    out["rows"] = np.round(np.nonzero(t_mask)[0][sel] / t_mask.shape[0], 4).tolist()   # 归一化行号（定前后位置用）
    out["px"] = np.stack(np.nonzero(t_mask), -1)[sel].tolist()   # 像素位置（行, 列）：_main 里按目标图取色
    rock = t_mask & fin & (np.abs(cosu) < 0.45)
    if rock.sum() < 200:
        return out
    tilt = float(np.degrees(np.arcsin(np.clip(np.median(cosu[rock]), -1, 1))))   # >0：崖面朝上外倾（上收下放）
    nx, ny = nn @ right.astype(np.float32), nn @ fwd.astype(np.float32)
    az = (np.degrees(np.arctan2(ny, nx)) + 360) % 360
    bins = (az // 30).astype(int)
    widths, rough, wts = [], [], []
    for b in range(12):
        lab, k = _label(rock & (bins == b))
        for i in range(1, k + 1):
            m = lab == i
            c = int(m.sum())
            if c < 30:
                continue
            pts = world(P[m])
            nm = nn[m]
            mean = nm.mean(0)
            mean /= np.linalg.norm(mean) + 1e-9
            th = np.arctan2(float(mean @ fwd), float(mean @ right))
            tang = np.array([-np.sin(th), np.cos(th)])
            proj = pts[:, :2] @ tang
            widths.append(float(np.percentile(proj, 95) - np.percentile(proj, 5)))
            rough.append(float(np.degrees(np.arccos(np.clip(nm @ mean, -1, 1))).mean()))
            wts.append(c)
    cliff = {"tilt_deg": round(tilt, 1), "pixels": int(rock.sum())}
    # 块面起伏：每块崖面的朝向偏离「这一段崖的总体朝向」多少度（低多边形崖的块面感就来自这个偏角）。
    # 总体朝向 = 法线水平分量在大窗口（画面宽 1/12）里的平均；偏角取均方根
    def box(a, r):
        c = np.cumsum(np.cumsum(np.pad(a, ((1, 0), (1, 0))), 0), 1)
        H_, W_ = a.shape
        y0 = np.clip(np.arange(H_) - r, 0, H_); y1 = np.clip(np.arange(H_) + r + 1, 0, H_)
        x0 = np.clip(np.arange(W_) - r, 0, W_); x1 = np.clip(np.arange(W_) + r + 1, 0, W_)
        return c[y1][:, x1] - c[y0][:, x1] - c[y1][:, x0] + c[y0][:, x0]
    hl = np.hypot(nx, ny) + 1e-9
    ux, uy = np.where(rock, nx / hl, 0), np.where(rock, ny / hl, 0)
    rr = max(3, rock.shape[1] // 24)
    sx, sy = box(ux, rr), box(uy, rr)
    sl = np.hypot(sx, sy) + 1e-9
    dots = np.clip((ux * sx + uy * sy) / sl, -1, 1)[rock]
    dev_h = float(np.sqrt(np.mean(np.degrees(np.arccos(dots)) ** 2)))
    tilt_all = np.degrees(np.arcsin(np.clip(cosu[rock], -1, 1)))
    dev_v = float(np.std(tilt_all))
    cliff.update(facet_dev_deg=round(float(np.hypot(dev_h, dev_v)), 1), facet_dev_h_deg=round(dev_h, 1),
                 facet_dev_v_deg=round(dev_v, 1))
    if widths:
        o = np.argsort(widths)
        cw = np.cumsum(np.asarray(wts, float)[o])
        mid = o[int(np.searchsorted(cw, cw[-1] / 2))]
        cliff.update(facet_m=round(widths[mid], 3), roughness_deg=round(float(np.average(rough, weights=wts)), 2),
                     facets=len(widths))
    out["cliff"] = cliff
    return out


def measure(npz: Path, instances: dict[str, list[dict]], backdrop_ids: set[str],
            boxes: dict[str, list[float]] | None = None, terrain_ids: set[str] | None = None,
            regions_out: dict | None = None) -> dict:
    """instances：资产 id → [{"mask": 路径, "score": 置信度}]（SAM 3 实例）。backdrop_ids：远景层资产（不当作物件挡掉支撑面）。"""
    P, valid, K, N = _load(npz)
    H, W = P.shape[:2]
    masks: list[tuple[str, int, float, np.ndarray]] = []
    for aid, items in instances.items():
        for k, it in enumerate(items):
            m = _mask(it["mask"], (H, W)) & valid
            if m.sum() >= 60:
                m = _erode(m)
            if m.sum() >= 20:
                masks.append((aid, k, float(it.get("score", 0)), m))
    # 同一块像素被两类资产认领（例如条纹灯塔也被认成风车）：只留置信度高的那个
    masks.sort(key=lambda t: -t[2])
    kept: list[tuple[str, int, float, np.ndarray]] = []
    for t in masks:
        if all((t[3] & k[3]).sum() < 0.6 * min(t[3].sum(), k[3].sum()) for k in kept):
            kept.append(t)
    objects = np.zeros((H, W), bool)
    for aid, _, _, m in kept:
        if aid not in backdrop_ids:
            objects |= m
    # 支撑平面：不属于任何物件的有效像素（海面 / 地面 / 远景面），只用离相机近的一半（远处掠射角的点会把平面拉歪）
    free = valid & ~objects
    src = "free-near"
    if free.sum() < 400:   # 画面被物件占满（如平地上的场景）：退回画面下部所有有效点
        free = valid.copy()
        free[: H // 2] = False
        src = "lower-half"
    via = plane_from_normals(P, N, free) if N is not None else None
    z = P[..., 2]
    near = free & (z <= np.percentile(z[free], 50))
    pts = P[near]
    n, d, frac = fit_plane(pts)
    if n[1] > 0:   # 法线朝上（OpenCV 相机 y 朝下）
        n, d = -n, -d
    # 支撑面是最低的那片大平面（海面 / 地面）：SAM 漏认的岛顶、台地也可能是平面。
    # 若还有大量空白像素落在拟合平面下方，说明拟合到了高处的台面，改用下方的点重拟合。
    cand = P[free]
    for _ in range(3):
        dist = cand @ n + d
        tol = 0.02 * (float(np.percentile(np.abs(dist), 90)) + 1e-6)
        below = cand[dist < -max(tol, 0.05)]
        if len(below) < max(400, 0.1 * len(cand)):
            break
        n2, d2, frac2 = fit_plane(below)
        if n2[1] > 0:
            n2, d2 = -n2, -d2
        if abs(n2 @ n) < 0.9:   # 下方点拟合出的不是同向平面（如崖壁）：保持原平面
            break
        n, d, frac = n2, d2, frac2
        src += "+lowest"
    if via is not None:
        n, d, src = via[0], via[1], via[2]
        frac = float((np.abs(P[free] @ n + d) < 0.05 * (np.abs(d) + 1e-6)).mean())
    fwd = np.array([0.0, 0.0, 1.0]) - n * n[2]
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, n)
    origin = -d * n

    def world(p):
        q = np.asarray(p, dtype=np.float64) - origin
        return np.stack([q @ right, q @ fwd, q @ n], -1)

    eye = world(np.zeros(3))
    look = np.array([np.array([0, 0, 1.0]) @ right, np.array([0, 0, 1.0]) @ fwd, np.array([0, 0, 1.0]) @ n])
    fov = float(np.degrees(2 * np.arctan(0.5 / (K[0, 0] if W >= H else K[1, 1]))))
    cam_up = n.astype(np.float32)

    def stats(k, score, m):
        q = world(P[m])
        lo, hi = np.percentile(q, 2, 0), np.percentile(q, 98, 0)
        return {
            "k": k, "score": round(score, 3), "pixels": int(m.sum()),
            "center": [round(float(np.median(q[:, 0])), 3), round(float(np.median(q[:, 1])), 3)],
            "base_z": round(float(lo[2]), 3), "top_z": round(float(hi[2]), 3),
            "median_z": round(float(np.median(q[:, 2])), 3),
            "front_y": round(float(lo[1]), 3),   # 看得见的正面：单视角下物体中心在它后面半个进深
            "footprint": [round(float(hi[0] - lo[0]), 3), round(float(hi[1] - lo[1]), 3)],
            "height": round(float(hi[2] - lo[2]), 3),
            "surface_z": round(float(np.percentile(q[:, 2], 85)), 3),   # 顶面大致高度（给立在上面的物件当承托面）
            "base_ray": base_ray(m),
            "base_v": round(float(np.percentile(np.nonzero(m)[0], 97)) / H, 4),
            **top_rows(m),
        }

    def top_rows(m):
        """顶面（朝上的水平像素）在画面里的上下边（归一化行号）：立在顶面上的物件按接地行号在前后沿之间插值定进深位置。"""
        if N is None:
            return {}
        nn = N[m]
        ok = np.isfinite(nn).all(-1)
        if ok.sum() < 50:
            return {}
        up = np.zeros(len(nn), bool)
        up[ok] = (nn[ok] / (np.linalg.norm(nn[ok], axis=1, keepdims=True) + 1e-9)) @ cam_up > np.cos(np.radians(15))
        zq = world(P[m])[:, 2]
        high = zq > 0.6 * float(np.percentile(zq, 98))   # 只要高处的水平面（岛顶 / 台面），不要水边的泡沫、码头
        rows = np.nonzero(m)[0][up & high]
        if len(rows) < 50:
            return {}
        cols = np.nonzero(m)[1][up & high]
        vb = float(np.percentile(rows, 3))
        ub = float(np.median(cols[rows <= vb + 2])) if (rows <= vb + 2).any() else float(np.median(cols))
        dc = np.array([((ub + 0.5) / W - K[0, 2]) / K[0, 0], ((vb + 0.5) / H - K[1, 2]) / K[1, 1], 1.0])
        dw = np.array([dc @ right, dc @ fwd, dc @ n]); dw /= np.linalg.norm(dw)
        return {"top_rows": [round(vb / H, 4), round(float(np.percentile(rows, 97)) / H, 4)],
                "top_surface_z": round(float(np.median(zq[up & high])), 3),
                "back_ray": [round(float(x), 5) for x in dw]}   # 顶面后沿的视线：与顶面相交得到地形的后边

    def base_ray(m):
        """物件最下沿（接地处）像素的视线方向（世界系单位向量）：位置用「视线 ∩ 承托平面」算，不靠这一点的深度。"""
        vs, us = np.nonzero(m)
        vb = np.percentile(vs, 97)
        sel = vs >= vb - 1
        u, v = float(np.median(us[sel])), float(np.max(vs[sel]))
        dc = np.array([((u + 0.5) / W - K[0, 2]) / K[0, 0], ((v + 0.5) / H - K[1, 2]) / K[1, 1], 1.0])
        dw = np.array([dc @ right, dc @ fwd, dc @ n])
        dw /= np.linalg.norm(dw)
        return [round(float(x), 5) for x in dw]

    zw = world(P[valid])[:, 2]
    zmap = np.full((H, W), -1e9, np.float32)
    zmap[valid] = zw
    scale_m = float(np.percentile(np.abs(zw), 90)) + 1e-6
    # SAM 没认出、或代表实例远小于拆分框：用框内「不属于其他物件、也不贴着水面」的像素补救。
    # 地形、远景不用框（框太大）；地形交给下面的残余地块。
    # 关键：框补救不得清空该类已有的 SAM 实例（实测柏树 27→1、花丛 13→1，控制台只剩一棵树）。
    #   · 已有实例 → 全部保留；框像素只并进 objects（别被残余地块吞），必要时把框尺寸元数据挂到 k=0 供 apply 校正 size；
    #   · 完全没有实例 → 才用框作为唯一实例。
    from_box, box_px = set(), {}
    for aid, box in (boxes or {}).items():
        if aid in backdrop_ids or aid in (terrain_ids or set()) or not box or len(box) != 4:
            continue
        x0, y0, x1, y1 = (int(round(box[0] * W)), int(round(box[1] * H)), int(round(box[2] * W)), int(round(box[3] * H)))
        if x1 - x0 < 3 or y1 - y0 < 3:
            continue
        area = (x1 - x0) * (y1 - y0)
        existing = [t for t in kept if t[0] == aid]
        # 任一实例已盖住框内 ≥20%：无需框补救
        if existing and max(t[3][y0:y1, x0:x1].sum() for t in existing) >= 0.2 * area:
            continue
        bm = np.zeros((H, W), bool)
        bm[y0:y1, x0:x1] = True
        others = np.zeros((H, W), bool)
        for oid, _, _, om in kept:
            if oid != aid:
                others |= om
        bm &= valid & ~others & (zmap > 0.02 * scale_m)
        if bm.sum() < 30:
            continue
        if existing:
            # 保留全部 SAM 实例；框只扩 objects，并把尺寸线索挂到代表实例（不改 mask、不删实例）
            objects |= bm
            # 框与某个实例有实质重叠时，才用框校正 size（避免 LLM 框在别处时量错）
            if max(t[3][y0:y1, x0:x1].sum() for t in existing) >= 0.05 * area:
                from_box.add(aid)
                box_px[aid] = ((x0 + x1) / 2, y1 - 1)
            continue
        # 完全没有 SAM 实例：框作为唯一来源
        kept.append((aid, 0, 0.5, _erode(bm)))
        from_box.add(aid)
        box_px[aid] = ((x0 + x1) / 2, y1 - 1)
        objects |= bm
    # 残余地块：既不是物件、也不在支撑平面上的有效像素（SAM 没认出岛 / 地形时，用它来量地形）
    residual = valid & ~objects & (zmap > 0.03 * scale_m)
    on_water = valid & (np.abs(zmap) <= 0.02 * scale_m)   # 水面 / 地面（下面再并上深度漂高的那部分海面）
    if N is not None and residual.sum() >= 200:
        # 深度在远近海面上会漂高零点几米：朝上、又贴着水面（低于地形顶高 15%）的像素是海，不是地形。
        # 否则岛的宽度、进深会把周围一圈海也算进去（实测把岛宽量大了一半）。崖脚是竖直面，不受影响。
        nn_ = N / (np.linalg.norm(N, axis=-1, keepdims=True) + 1e-9)
        upm = np.nan_to_num(nn_ @ n.astype(np.float32)) > 0.8
        zt = float(np.percentile(zmap[residual], 98))
        drift = residual & upm & (zmap < 0.15 * zt)
        residual &= ~drift
        on_water |= drift   # 漂高的海面也是海面：判断远景层是否躺在水面上、取水色时都算它
    residual = _erode(residual, 2)
    residual = _largest_blob(residual)   # 远处海面因深度漂移也会高出平面一点：只留连成一片的最大块（岛 / 地块本身）
    # SAM 认出的地形常常只是崖壁（「island cliff」），岛顶草地不在里面：地形 = SAM 实例 ∪ 残余地块。
    # 否则岛的高度图只剩崖顶边缘、顶面颜色量成崖色（实测岛顶取成了粉色）。
    if residual.sum() >= 200 and terrain_ids:
        for t, (aid, k, sc, m) in enumerate(kept):
            if k == 0 and aid in terrain_ids:
                # SAM 认出的「岛」只是远处一小块岬角、和主陆块不相连（被花草树木盖满的岛 SAM 常认不出）：
                # 地形就是主陆块本身，不把那块岬角并进来（否则顶面、崖壁区域会量到远处海岸的颜色）
                kept[t] = (aid, k, sc, residual if terrain_detached(m, residual) else m | residual)
    out: dict[str, list[dict]] = {}
    if residual.sum() >= 200:
        out["__residual__"] = [stats(0, 0.0, residual)]
    for aid, k, score, m in kept:
        row = stats(k, score, m)
        # 框尺寸元数据只挂到代表实例（k==0），其它 SAM 实例保持自身几何，供 placements 使用
        if aid in from_box and k == 0:
            # 框里混着后面的地形：接地处取拆分框底边中点，高度在 apply 里按承托面重算
            u, v = box_px[aid]
            dc = np.array([((u + 0.5) / W - K[0, 2]) / K[0, 0], ((v + 0.5) / H - K[1, 2]) / K[1, 1], 1.0])
            dw = np.array([dc @ right, dc @ fwd, dc @ n]); dw /= np.linalg.norm(dw)
            ui, vi = int(u), int(v)
            patch = zmap[max(0, vi - 3):vi + 1, max(0, ui - 2):ui + 3]
            patch = patch[patch > -1e8]
            bx = boxes[aid]
            row.update({"from": "box", "box_frac": [round(bx[2] - bx[0], 4), round(bx[3] - bx[1], 4)],
                        "base_ray": [round(float(x), 5) for x in dw], "base_v": round(v / H, 4),
                        **({"base_z": round(float(np.median(patch)), 3)} if patch.size else {})})
        out.setdefault(aid, []).append(row)
    # 远景层是不是「躺在支撑面上」（海面 / 湖面 / 地面）：看它拆分框里有多少有效像素就在平面上
    plane_frac = {}
    for aid in backdrop_ids:
        box = (boxes or {}).get(aid)
        if len(backdrop_ids) == 1:
            # 只有一个远景层：画面里不属于物件、地形的部分都归它（拆分给的框常常只框了天空，海面在框外）
            reg = np.ones((H, W), bool)
        elif box and len(box) == 4:
            x0, y0, x1, y1 = (int(round(box[0] * W)), int(round(box[1] * H)), int(round(box[2] * W)), int(round(box[3] * H)))
            reg = np.zeros((H, W), bool)
            reg[y0:y1, x0:x1] = True
        else:
            continue
        on = reg & ~objects & ~residual & on_water
        tot = reg & valid & ~objects & ~residual
        if tot.sum() >= 50:
            plane_frac[aid] = round(float(on.sum() / tot.sum()), 3)
    for v in out.values():   # 保持传入顺序：拆分阶段已把核验过的代表实例排在最前
        v.sort(key=lambda r: r["k"])
    t_mask = next((m for aid, k, _, m in kept if k == 0 and aid in (terrain_ids or set())), None)
    if t_mask is None and residual.sum() >= 200:
        t_mask = residual
    shape = terrain_shape(P, N, t_mask, world, n, right, fwd) if t_mask is not None and t_mask.sum() >= 200 else None
    if regions_out is not None:
        # 配色环节要用的区域（分析分辨率）：每个物件的代表掩码、地形（再按法线分崖壁 / 顶面）、水面、天空
        regions_out.update(asset_regions(kept, backdrop_ids, terrain_ids))
        if t_mask is not None:
            regions_out["terrain"] = t_mask
            if N is not None:
                nn = N / (np.linalg.norm(N, axis=-1, keepdims=True) + 1e-9)
                cosu = np.nan_to_num(nn @ n.astype(np.float32))
                regions_out["terrain_rock"] = _erode(t_mask & (np.abs(cosu) < 0.45))
                regions_out["terrain_top"] = terrain_top_mask(t_mask, cosu, zmap)
        regions_out["water"] = _erode(~objects & ~residual & on_water, 2)
        sky = ~valid
        sky[int(H * 0.6):] = False
        regions_out["sky"] = _erode(sky, 2)
    pitch = float(np.degrees(np.arcsin(max(-1.0, min(1.0, -look[2])))))
    return {"plane": {"source": src, "inlier_frac": round(frac, 3)}, "backdrop_on_plane_frac": plane_frac,
            "frame": {"right": [float(x) for x in right], "fwd": [float(x) for x in fwd], "up": [float(x) for x in n]},
            "camera": {"eye": [round(float(x), 3) for x in eye], "look": [round(float(x), 4) for x in look],
                       "fy_n": round(float(K[1, 1]), 5), "cy_n": round(float(K[1, 2]), 5),
                       "fov_deg": round(fov, 2), "pitch_down_deg": round(pitch, 2)},
            "instances": out, "image_px": [W, H], **({"terrain_shape": shape} if shape else {})}


def depth_fraction(rows, rows_a, cam: dict):
    """顶面上一点的前后位置（0 = 顶面前沿，1 = 后沿），按透视换算：同一水平面上，距离 ∝ 1 / (行号 − 地平线行号)。
    顶面几乎贴着视线时，靠近地平线的一行代表的距离远大于靠近画面下方的一行；按行号线性插值会把后半个岛压扁。
    只用相机的俯角与内参（实测），不用这一点自己的深度（贴着视线时最不准）。"""
    rows = np.asarray(rows, np.float64)
    front, back = float(rows_a[1]), float(rows_a[0])
    fy, cy = cam.get("fy_n"), cam.get("cy_n")
    if not fy or cy is None:
        return np.clip((front - rows) / max(front - back, 1e-6), 0, 1)
    hz = float(cy) - float(fy) * np.tan(np.radians(float(cam.get("pitch_down_deg", 0.0))))
    lo = hz + 0.25 * (back - hz)   # 比后沿更靠近地平线的行不再外推
    dist = lambda r: 1.0 / np.maximum(r - hz, lo - hz)
    d0, d1 = dist(front), dist(back)
    if d1 - d0 < 1e-6:
        return np.clip((front - rows) / max(front - back, 1e-6), 0, 1)
    return np.clip((dist(rows) - d0) / (d1 - d0), 0, 1)


def relief(shape: dict, anchor_size, ax: float, rows_a=None, eye=None, ay: float = 0.0, n: int = 24,
           y_off: float = 0.0, cam: dict | None = None) -> dict | None:
    """地形点云 → 俯视高度图（Oskar 引擎的地形约束）。坐标归一化到 [-1, 1]，半宽 = 地形水平最大边的一半：
    u = x / half（右为正），v = y / half（远离相机为正）；h = 相对高度 0..1（0 = 水面 / 地面，1 = 地形顶）。
    x 用实测（宽度可靠）；y 方向单视角深度最不准，只取点的前后次序，按实测进深重新铺开。看不见的格子（被顶上物件挡住、
    轮廓里的洞）按周围格子补：只补轮廓内（行、列两个方向都夹在有点的格子之间）。"""
    pts = np.asarray(shape.get("points") or [], dtype=np.float64)
    if len(pts) < 200:
        return None
    tw, td, th = (float(x) for x in anchor_size)
    half = max(tw, td) / 2
    z0 = float(np.percentile(pts[:, 2], 2))
    upf = np.asarray(shape.get("up") or [], bool)
    rows = np.asarray(shape.get("rows") or [], np.float64)
    keep = np.ones(len(pts), bool)
    if len(upf) == len(pts) and upf.sum() >= 150:
        keep = upf.copy()
        land = keep & ((pts[:, 2] - z0) >= 0.15 * th)   # 地表点里贴着水面的是海（深度漂移），不算陆地
        if land.sum() >= 100:
            keep = land
    bare = np.asarray(shape.get("bare") or [], np.float64)
    if rows_a and len(rows) == len(keep):
        # 行号插值只对顶面成立：落在顶面前沿以下（崖脚的礁石、浪花）或后沿以上的点不进高度图
        keep = keep & (rows <= rows_a[1] + 0.01) & (rows >= rows_a[0] - 0.01)
    bare = bare[keep] if len(bare) == len(keep) else None
    pts = pts[keep]
    rows = rows[keep] if len(rows) == len(keep) else np.zeros(0)
    x = pts[:, 0] - ax
    if rows_a and rows_a[1] - rows_a[0] > 0.02 and len(rows) == len(pts):
        # 前后位置与物件摆放同一个算法：顶面上一点的行号在顶面前沿（画面靠下）与后沿之间线性插值
        f = depth_fraction(rows, rows_a, cam or {})
        y = -td / 2 + f * td
        if eye is not None:
            # 左右位置也要跟着改：x 是由（不准的）深度推出来的，深度偏大 x 就按同一比例偏大。按插值后的距离等比还原
            dm = pts[:, 1] - eye[1]
            de = (ay + y) - eye[1]
            ok_ = dm > 0.5
            x = np.where(ok_, (pts[:, 0] - eye[0]) * np.where(ok_, de / np.where(ok_, dm, 1), 1) + eye[0], pts[:, 0]) - ax
    else:
        lo, hi = np.percentile(pts[:, 1], [2, 98])
        y = ((pts[:, 1] - lo) / max(hi - lo, 1e-6) - 0.5) * td
    y = y - y_off
    hz = np.clip((pts[:, 2] - z0) / max(th, 1e-6), 0, 1)
    u, v = x / half, y / half
    ok = (np.abs(u) <= 1) & (np.abs(v) <= 1)   # 出界的点丢掉（不要压到边上一列）
    u, v, hz = u[ok], v[ok], hz[ok]
    if bare is not None:
        bare = bare[ok]
    j = np.minimum(((u + 1) / 2 * n).astype(int), n - 1)
    i = np.minimum(((v + 1) / 2 * n).astype(int), n - 1)
    cnt = np.zeros((n, n), int)
    hmax = np.full((n, n), np.nan)
    for a, b, h in zip(i, j, hz):
        cnt[a, b] += 1
    for a in range(n):
        for b in range(n):
            if cnt[a, b]:
                hmax[a, b] = float(np.percentile(hz[(i == a) & (j == b)], 90))
    occ = cnt >= 2
    lab, k = _label(occ)   # 只留连成一片的最大块（岛本身）；离开主体的零星格多半是水边泡沫或别的物件
    if k > 1:
        sizes = [(lab == t).sum() for t in range(1, k + 1)]
        occ = lab == (int(np.argmax(sizes)) + 1)
    hmax[~occ] = np.nan
    rows = np.zeros_like(occ)
    cols = np.zeros_like(occ)
    for a in range(n):
        idx = np.nonzero(occ[a])[0]
        if len(idx):
            rows[a, idx.min():idx.max() + 1] = True
        idx = np.nonzero(occ[:, a])[0]
        if len(idx):
            cols[idx.min():idx.max() + 1, a] = True
    def grow(m, r):
        o = m.copy()
        for _ in range(r):
            e = o.copy()
            e[1:] |= o[:-1]; e[:-1] |= o[1:]; e[:, 1:] |= o[:, :-1]; e[:, :-1] |= o[:, 1:]
            o = e
        return o
    closed = ~grow(~grow(occ, 2), 2)   # 闭运算：补上被顶上物件挡住的缺口
    inside = occ | (rows & cols) | closed
    # 顶面后半部分常被顶上的灯塔、房子挡住，低机位下平面形状也几乎看不出来：看得见的格子照实测；
    # 看不见的部分按实测宽 × 实测进深的椭圆补（前沿位置、左右范围、总进深都是实测，只有「后半边是圆弧」是假设，
    # 补的格子数记入 back_inferred_cells）
    back_inferred = 0
    if rows_a and inside.any():
        cols_seen = np.nonzero(inside.any(0))[0]
        i0 = int(np.nonzero(inside.any(1))[0].min())
        cj = (cols_seen.min() + cols_seen.max() + 1) / 2
        ci = i0 + td / (2 * half) * n / 2
        rx, ry = tw / (2 * half) * n / 2, td / (2 * half) * n / 2
        for a in range(n):
            for b in range(n):
                if not inside[a, b] and ((b + 0.5 - cj) / rx) ** 2 + ((a + 0.5 - ci) / ry) ** 2 <= 1.0:
                    inside[a, b] = True
                    back_inferred += 1
    filled = hmax.copy()
    for _ in range(2 * n):   # 补洞：反复取四邻已知格的平均
        miss = inside & np.isnan(filled)
        if not miss.any():
            break
        nxt = filled.copy()
        for a, b in zip(*np.nonzero(miss)):
            nb = [filled[aa, bb] for aa, bb in ((a - 1, b), (a + 1, b), (a, b - 1), (a, b + 1))
                  if 0 <= aa < n and 0 <= bb < n and not np.isnan(filled[aa, bb])]
            if nb:
                nxt[a, b] = float(np.mean(nb))
        filled = nxt
    grid = [[(round(float(filled[a, b]), 3) if inside[a, b] and not np.isnan(filled[a, b]) else None) for b in range(n)]
            for a in range(n)]
    bare_g = None
    if bare is not None:
        bare_g = [[0] * n for _ in range(n)]
        for a in range(n):
            for b in range(n):
                sel_ = (i == a) & (j == b)
                if grid[a][b] is not None and sel_.sum() >= 2 and bare[sel_].mean() > 0.5:
                    bare_g[a][b] = 1
    occ_b = np.array([[grid[a][b] is not None for b in range(n)] for a in range(n)])
    cols_, rows_ = np.nonzero(occ_b.any(0))[0], np.nonzero(occ_b.any(1))[0]
    cell = 2 * half / n
    center = [round(float(((cols_.min() + cols_.max() + 1) / 2 - n / 2) * cell), 3) if len(cols_) else 0.0,
              round(float(((rows_.min() + rows_.max() + 1) / 2 - n / 2) * cell), 3) if len(rows_) else 0.0]
    return {"n": n, "half_m": round(half, 3), "size_m": [tw, td, th], "h": grid, "center_m": center, **({"bare": bare_g} if bare_g else {}),
            "land_cells": int(inside.sum()), "seen_cells": int(occ.sum()), "back_inferred_cells": back_inferred, **({"cliff": shape["cliff"]} if shape.get("cliff") else {})}


def apply(spec: dict, meas: dict, aspect: float) -> dict:
    """把测量结果写进拆分方案：尺寸、摆放、相机都以测量为准；LLM 只保留「有哪些物件」与朝向。返回摘要。"""
    assets = {a["id"]: a for a in spec["assets"]}
    inst = meas["instances"]
    back = {k for k, a in assets.items() if a.get("method") == "backdrop" or a.get("kind") == "backdrop"}
    terrain_ids = [k for k, a in assets.items() if k not in back and
                   (a.get("method") == "terrain" or a.get("kind") == "environment")]
    if "__residual__" in inst:
        for k in terrain_ids:
            if k not in inst:   # SAM 没认出地形：用残余地块量它
                inst[k] = inst["__residual__"]
                assets[k]["measured_from"] = "residual"
                break
    inst = {k: v for k, v in inst.items() if k in assets}
    solid = [k for k in inst if k not in back]
    if not solid:
        raise ValueError("深度定位：没有任何可测量的物件实例")
    # 锚点 = 占地最大的地形 / 环境件（没有就取占地最大的物件）：把它的中心平移到原点
    def area(k):
        r = inst[k][0]
        return r["footprint"][0] * max(r["footprint"][1], 1e-3)
    terrain = [k for k in solid if assets[k].get("method") == "terrain" or assets[k].get("kind") == "environment"]
    anchor = max(terrain or solid, key=area)
    llm_a = [float(x) for x in (assets[anchor].get("size_m") or [1, 1, 1])]
    r_a = inst[anchor][0]
    dep_a = r_a["footprint"][0] * (llm_a[1] / max(llm_a[0], 1e-6))
    ax, ay = r_a["center"][0], r_a["front_y"] + dep_a / 2
    ray_a = r_a.get("base_ray")
    if ray_a and ray_a[2] < -1e-3:   # 地形的接地处（崖脚与水面交线）用视线 ∩ 水面，比崖脚那一点的深度可靠
        t_a = -meas["camera"]["eye"][2] / ray_a[2]
        front = meas["camera"]["eye"][1] + t_a * ray_a[1]
        ay = front + dep_a / 2
        back_ray, top_z = r_a.get("back_ray"), r_a.get("top_surface_z")
        if back_ray and top_z and back_ray[2] < -1e-3 and top_z < meas["camera"]["eye"][2]:
            # 看得见顶面时：后沿 = 顶面最远一行的视线 ∩ 顶面高度。进深 = 后沿 − 前沿，量出来的，不再借 LLM 的长宽比
            back_y = meas["camera"]["eye"][1] + (top_z - meas["camera"]["eye"][2]) / back_ray[2] * back_ray[1]
            # 顶面后沿在地平线附近时视线几乎贴着顶面，交点很不稳：只接受不超过实测宽度的结果
            if 0.3 * r_a["footprint"][0] < back_y - front <= 1.0 * r_a["footprint"][0]:
                dep_a = back_y - front
                ay = (front + back_y) / 2
                assets[anchor]["_measured_depth"] = round(dep_a, 3)
    llm_w = llm_a[0] or 1.0
    eye0 = meas["camera"]["eye"]
    surf = r_a.get("top_surface_z", r_a.get("surface_z", 0))
    supports = [0.0] + ([surf] if surf > 0.05 else [])

    rows_a = r_a.get("top_rows")

    def contact(r, dep, is_anchor):
        """接地点 = 最下沿像素的视线与承托平面（水面 z=0，或地形顶面）的交点；中心在接地点后面半个进深。
        深度只用来判断它立在哪个承托面上。视线不朝下（接地处被挡住 / 在地平线上方）时退回深度测量。"""
        h = 0.0 if is_anchor else min(supports, key=lambda z: abs(z - r["base_z"]))
        if h > 0 and rows_a and rows_a[1] - rows_a[0] > 0.02:
            # 立在地形顶面上：顶面几乎与视线平行，「视线 ∩ 平面」和深度都不稳；按接地行号在顶面前后沿之间插值
            f = float(depth_fraction([r["base_v"]], rows_a, {**meas["camera"]})[0])
            td_ = dep_a
            y = -td_ / 2 + f * td_
            # 左右位置与地形高度图同一种换算：深度改了，x 按到相机的距离等比改
            dm = r["center"][1] - eye0[1]
            cx = r["center"][0]
            if dm > 0.5:
                cx = eye0[0] + (cx - eye0[0]) * ((ay + y) - eye0[1]) / dm
            return [round(cx - ax, 3), round(y, 3), round(h, 3)]
        ray = r.get("base_ray")
        if ray and ray[2] < -1e-3:
            t = (h - eye0[2]) / ray[2]
            if t > 0:
                cx, cy = eye0[0] + t * ray[0], eye0[1] + t * ray[1]
                if is_anchor:   # 锚点（地形）左右位置就是它的中心；最下沿那一点可能在岛的一侧
                    cx = r["center"][0]
                return [round(cx - ax, 3), round(cy + dep / 2 - ay, 3), round(h, 3)]
        return [round(r["center"][0] - ax, 3), round(r["front_y"] + dep / 2 - ay, 3), round(r["base_z"], 3)]
    s = inst[anchor][0]["footprint"][0] / llm_w   # 只给没测到的物件按同一比例缩放
    box_pending = []
    summary = {"anchor": anchor, "scale_vs_llm": round(s, 3), "measured": [], "scaled_only": []}
    for k, a in assets.items():
        llm = [float(x) for x in (a.get("size_m") or [1, 1, 1])]
        rot = float((a.get("placements") or [{}])[0].get("rot_z", 0) or 0)
        if k in inst and k not in back:
            # 重复物件（柏树、花丛等）：placements 跟 SAM 找到的全部实例走，不要被 LLM「只摆一个代表」截断。
            # 地形 / 环境件仍只取 1 个（主陆块）；其余听实测实例数与 LLM 数量的较大者。
            is_terrain = a.get("method") == "terrain" or a.get("kind") == "environment"
            want = 1 if is_terrain else max(len(inst[k]), len(a.get("placements") or []), 1)
            rows = inst[k][:want]
            r0 = rows[0]
            w, h = r0["footprint"][0], r0["height"]
            # 单视角测不出前后进深（只看到朝向相机的表面，轮廓处还混着背景）：进深 = 实测宽 × LLM 的进深/宽比
            dep = a.pop("_measured_depth", None) if k == anchor else None
            dep = dep or w * (llm[1] / max(llm[0], 1e-6))
            a["size_m_llm"] = llm
            a["size_m"] = [round(max(w, 0.05), 3), round(max(dep, 0.05), 3), round(max(h, 0.05), 3)]
            a["placements"] = [{"at": contact(r, dep, k == anchor), "rot_z": rot, "scale": 1} for r in rows]
            if r0.get("from") == "box" and r0.get("box_frac"):
                box_pending.append((k, r0))
            a["measured"] = {"instances": len(rows), "source": "depth" if r0.get("from") != "box" else "depth+box",
                             **({"from": a.pop("measured_from")} if a.get("measured_from") else {})}
            summary["measured"].append(k)
        elif k in back:
            rows = inst.get(k) or []
            scene_w = inst[anchor][0]["footprint"][0]
            pf = (meas.get("backdrop_on_plane_frac") or {}).get(k)
            on_plane = (bool(rows) and abs(rows[0]["median_z"]) < 0.05 * max(scene_w, 1.0)) or (pf is not None and pf >= 0.3)
            a["measured"] = {"source": "depth" if rows else "none", "on_plane": on_plane,
                             **({"median_z": rows[0]["median_z"]} if rows else {})}
        else:   # 没测到（SAM 没找到）：沿用 LLM 的相对布局，按锚点比例换算到米制
            a["size_m_llm"] = llm
            a["size_m"] = [round(x * s, 3) for x in llm]
            a["placements"] = [{**p, "at": [round(float(c) * s, 3) for c in (p.get("at") or [0, 0, 0])]}
                               for p in a.get("placements") or []]
            a["measured"] = {"source": "llm-scaled"}
            summary["scaled_only"].append(k)
    # 承托约束：底部高出水面、立在地形上的物件，中心必须落在地形占地范围内。
    # 单视角深度对「往画面里」的距离最不准（实测会把岛顶上的灯塔推到岛后面十几米），这里按几何关系收回来。
    tw, td, th = assets[anchor]["size_m"]
    clamped = []
    for k, a in assets.items():
        if k == anchor or k in back:
            continue
        w, d = a["size_m"][0], a["size_m"][1]
        for p in a.get("placements") or []:
            at = list(p.get("at") or [0, 0, 0])
            if len(at) < 3 or at[2] < 0.3 * th:
                continue
            bx, by = max(0.0, tw / 2 - w / 2), max(0.0, td / 2 - d / 2)
            nx, ny = min(max(at[0], -bx), bx), min(max(at[1], -by), by)
            if (nx, ny) != (at[0], at[1]):
                clamped.append(k)
            p["at"] = [round(nx, 3), round(ny, 3), at[2]]
    summary["clamped_onto_terrain"] = sorted(set(clamped))
    # 框量出的物件（框里混着后面的地形和天空边缘）：宽、高按框在画面里的张角 × 相机到它（最终位置）的距离换算
    fx_ = 2 * np.tan(np.radians(meas["camera"]["fov_deg"]) / 2)
    cam_ = meas["camera"]
    hz_row = None
    if cam_.get("fy_n") and cam_.get("cy_n") is not None:
        hz_row = float(cam_["cy_n"]) - float(cam_["fy_n"]) * np.tan(np.radians(float(cam_.get("pitch_down_deg", 0.0))))

    def plane_dist(row):
        """画面行号 → 视线落到水面（z=0）的水平距离（相机高 / 俯角正切）；行在地平线上方时为 None。"""
        if hz_row is None or row <= hz_row + 1e-3:
            return None
        return float(eye0[2]) / ((row - hz_row) / float(cam_["fy_n"]))

    for k, r0 in box_pending:
        a = assets[k]
        llm = a.get("size_m_llm") or a["size_m"]
        at0 = a["placements"][0]["at"]
        dist = float(np.hypot(at0[0] + ax - eye0[0], at0[1] + ay - eye0[1]))
        w_m = r0["box_frac"][0] * fx_ * dist
        h_m = r0["box_frac"][1] * fx_ / max(aspect, 1e-6) * dist
        dep_m = w_m * (llm[1] / max(llm[0], 1e-6))
        near = plane_dist(r0.get("base_v", 1.0))
        far = plane_dist(r0.get("base_v", 1.0) - r0["box_frac"][1])
        if len(at0) >= 3 and abs(at0[2]) < 0.05 and near and far and far > near:
            # 平躺在水面上的物件（码头、栈道）：框的上下沿都是水面上的点，进深 = 两条视线落水点的距离差（实测几何），
            # 不再用「宽 × LLM 的长宽比」（实测把码头算成 30 米长，中心也被推到岛旁边）；高度按 LLM 的高宽比
            dep_old = a["size_m"][1]
            dep_m = far - near
            mid = (near + far) / 2
            w_m = r0["box_frac"][0] * fx_ * mid
            h_m = w_m * (llm[2] / max(llm[0], 1e-6))
            for p in a["placements"][:1]:
                p["at"] = [p["at"][0], round(p["at"][1] - dep_old / 2 + dep_m / 2, 3), p["at"][2]]
        a["size_m"] = [round(max(w_m, 0.2), 3), round(max(dep_m, 0.2), 3), round(max(h_m, 0.2), 3)]
    # 地形形体：俯视高度图 + 崖壁测量，交给 Oskar 引擎当约束；立在地形顶上的物件 → 引擎预留平地（台地）
    shp = meas.get("terrain_shape")
    if shp and (assets[anchor].get("method") == "terrain" or assets[anchor].get("kind") == "environment"):
        rel = relief(shp, assets[anchor]["size_m"], ax, rows_a, eye0, ay, cam=meas["camera"])
        if rel and max(abs(c) for c in rel["center_m"]) > 0.5 * 2 * rel["half_m"] / rel["n"]:
            # 引擎做出的地形会按包围盒居中摆放：先把高度图挪到陆地正中，再把其他物件和相机一起挪同样的量，
            # 保证物件与岛的相对位置不变
            dx, dy = rel["center_m"]
            for k, a in assets.items():
                if k == anchor or k in back:
                    continue
                for p in a.get("placements") or []:
                    at = p.get("at") or []
                    if len(at) >= 2:
                        p["at"] = [round(at[0] - dx, 3), round(at[1] - dy, 3), *at[2:]]
            rel2 = relief(shp, assets[anchor]["size_m"], ax + dx, rows_a, eye0, ay, y_off=dy, cam=meas["camera"])
            ax, ay = ax + dx, ay + dy
            if rel2:
                rel = rel2
                rel["shifted_m"] = [dx, dy]
        if rel:
            half = rel["half_m"]
            pls = []
            for k, a in assets.items():
                if k == anchor or k in back:
                    continue
                for p in a.get("placements") or []:
                    at = p.get("at") or [0, 0, 0]
                    if len(at) >= 3 and at[2] >= 0.3 * th:
                        pls.append({"asset": k, "at": [round(at[0] / half, 4), round(at[1] / half, 4)],
                                    "r": round(float(1.1 * max(a["size_m"][0], a["size_m"][1]) / 2 / half), 4),
                                    "h": round(min(1.0, at[2] / max(th, 1e-6)), 3)})
            rel["plateaus"] = pls
            assets[anchor]["relief"] = rel
            summary["relief"] = {"land_cells": rel["land_cells"], "seen_cells": rel["seen_cells"],
                                 "plateaus": len(pls), **({"cliff": rel["cliff"]} if rel.get("cliff") else {})}
    cam = meas["camera"]
    eye = [cam["eye"][0] - ax, cam["eye"][1] - ay, cam["eye"][2]]
    dist = float(np.hypot(eye[0], eye[1])) or 10.0
    tgt = [eye[i] + cam["look"][i] * dist for i in range(3)]
    spec["camera_llm"] = spec.get("camera")
    spec["camera"] = {"eye": [round(x, 3) for x in eye], "target": [round(x, 3) for x in tgt],
                      "fov_deg": cam["fov_deg"], "aspect": round(float(aspect), 4)}
    if isinstance(spec.get("scene"), dict) and spec["scene"].get("size_m"):
        spec["scene"]["size_m_llm"] = spec["scene"]["size_m"]
        spec["scene"]["size_m"] = [round(float(x) * s, 3) for x in spec["scene"]["size_m"]]
    spec["depth_layout"] = {**summary, "plane": meas["plane"], "camera": spec["camera"],
                            "pitch_down_deg": cam["pitch_down_deg"]}
    return summary


def terrain_detached(sam_mask: np.ndarray, residual: np.ndarray, ratio: float = 0.25) -> bool:
    """SAM 的地形实例与主陆块（残余地块）不重叠、面积又小得多：它认的是别处的一块地。"""
    small = sam_mask.sum() < ratio * residual.sum()
    return bool(small and (sam_mask & residual).sum() < 0.1 * sam_mask.sum())


def asset_regions(kept, backdrop_ids, terrain_ids, rel: float = 0.6) -> dict:
    """配色区域：每个物件 = 它所有可信实例的并集（置信度不低于代表实例的 rel 倍），不只是代表实例。
    重复很多的东西（几十棵柏树、成片花丛）只取一个实例时，取色只看到画面的零点几，颜色又被边上的东西带偏。"""
    rep = {aid: sc for aid, k, sc, _ in kept if k == 0}
    out: dict = {}
    for aid, k, sc, m in kept:
        if aid in (terrain_ids or set()) or aid in backdrop_ids or aid not in rep:
            continue
        if k != 0 and sc < rel * rep[aid]:
            continue
        key = "asset:" + aid
        out[key] = m.copy() if key not in out else (out[key] | m)
    return out


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def main() -> None:
    try:
        _main()
    except Exception:   # 让管线拿到完整原因
        import traceback, sys
        traceback.print_exc()
        sys.exit(3)


def _main() -> None:
    """控制台的环境没有 numpy：管线以子进程调用本文件（envs/depth 的 python），只交换 JSON。
    python depth_layout.py IN.json OUT.json
    IN：{"image", "spec", "masks": {资产: [{"mask", "score"}]}, "aspect", "model", "work_dir"}
    OUT：{"spec": 写入测量后的方案, "summary", "measure", "how"}"""
    import sys
    req = json.loads(Path(sys.argv[1]).read_text())
    spec = req["spec"]
    npz, how = run_depth(req["image"], Path(req["work_dir"]), req.get("model", "da3"))
    back = {a["id"] for a in spec["assets"] if a.get("method") == "backdrop" or a.get("kind") == "backdrop"}
    terrain = {a["id"] for a in spec["assets"] if a.get("method") == "terrain" or a.get("kind") == "environment"}
    boxes = {a["id"]: a.get("bbox_llm") or a.get("bbox") for a in spec["assets"]}
    regions: dict = {}
    meas = measure(npz, req["masks"], back, boxes, terrain, regions_out=regions)
    shp = meas.get("terrain_shape")
    if shp and "terrain_rock" in regions and "terrain_top" in regions:
        try:   # 顶面每一点是草还是裸岩：颜色更接近崖面（实测）还是更接近顶面主体（实测）
            import color_palette as CP
            Hh, Ww = regions["terrain"].shape
            img = np.asarray(Image.open(req["image"]).convert("RGB").resize((Ww, Hh), Image.BILINEAR), np.float32) / 255
            lab_rock = CP.to_lab(np.median(img[regions["terrain_rock"]], 0))
            lab_top = CP.to_lab(np.median(img[regions["terrain_top"]], 0))
            px = np.asarray(shp.pop("px"), int)
            lab = CP.to_lab(img[px[:, 0], px[:, 1]])
            shp["bare"] = (np.linalg.norm(lab - lab_rock, axis=1) < np.linalg.norm(lab - lab_top, axis=1)).astype(int).tolist()
        except Exception as e:  # noqa: BLE001 — 取不到颜色就全按草顶
            print(f"[depth_layout] 顶面裸岩判定失败：{e}", file=sys.stderr)
    if shp:
        shp.pop("px", None)
    summary = apply(spec, meas, float(req.get("aspect", 1.0)))
    spec["depth_layout"]["models"] = how
    # 配色环节（取色 + 调色板）：在同一批区域上直接从目标图测颜色；验收时同机位渲染图也按这些区域比
    try:
        import color_palette as CP
        reg_dir = Path(req["work_dir"]) / "regions"
        reg_dir.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name, m in regions.items():
            if m.sum() >= 30:
                pth = reg_dir / (name.replace(":", "__") + ".png")
                Image.fromarray(m.astype(np.uint8) * 255).save(pth)
                paths[name] = str(pth)
        # 清掉本轮没写出的旧区域图（实测 terrain_top.png 残留未刷新会误导人眼核对）
        keep = {Path(p).name for p in paths.values()}
        for old in reg_dir.glob("*.png"):
            if old.name not in keep:
                old.unlink(missing_ok=True)
        spec["palette_measured"] = CP.measure(req["image"], paths, spec)
        for name in spec["palette_measured"].get("suspect", {}):
            paths.pop(name, None)
        spec["palette_measured"]["regions"] = paths
        if "terrain" in regions:
            light = CP.light_direction(req["image"], npz, regions["terrain"], meas["frame"])
            if light:
                spec["light_measured"] = light
    except Exception as e:  # noqa: BLE001 — 配色失败不影响尺寸与摆放
        import traceback
        traceback.print_exc()
        spec["palette_measured"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        preview(spec, req["image"], Path(req["work_dir"]) / "layout_preview.png")
    except Exception as e:  # noqa: BLE001 — 预览图失败不影响测量结果
        print(f"[depth_layout] 预览图失败：{e}", file=sys.stderr)
    write_json(Path(sys.argv[2]), {"spec": spec, "summary": summary, "measure": meas, "how": how})


def preview(spec: dict, image: str, out: Path) -> None:
    """目标图 + 俯视 + 侧视：每个物件按测得的尺寸画成方框，标出相机，方便人眼核对摆放。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for fp in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc"):
        if Path(fp).exists():
            font_manager.fontManager.addfont(fp)
            plt.rcParams["font.family"] = font_manager.FontProperties(fname=fp).get_name()
            break
    fig, ax = plt.subplots(1, 3, figsize=(21, 7), dpi=90)
    ax[0].imshow(Image.open(image).convert("RGB"))
    ax[0].set_title("目标图"); ax[0].axis("off")
    cmap = plt.get_cmap("tab10")
    cam = spec["camera"]
    for j, a in enumerate(x for x in spec["assets"] if not (x.get("method") == "backdrop" or x.get("kind") == "backdrop")):
        w, d, h = a["size_m"]
        src = (a.get("measured") or {}).get("source", "")
        for p in a.get("placements") or []:
            x, y, z = (p.get("at") or [0, 0, 0])[:3]
            c = cmap(j % 10)
            ls = "-" if src == "depth" else "--"
            ax[1].add_patch(plt.Rectangle((x - w / 2, y - d / 2), w, d, fill=False, ec=c, lw=2, ls=ls))
            ax[2].add_patch(plt.Rectangle((y - d / 2, z), d, h, fill=False, ec=c, lw=2, ls=ls))
            ax[1].annotate(a["id"], (x, y), fontsize=8, color=c)
            ax[2].annotate(f"{a['id']} {h:.1f}m", (y, z + h), fontsize=8, color=c)
    ax[1].plot(cam["eye"][0], cam["eye"][1], "k^", ms=10); ax[1].annotate("相机", cam["eye"][:2])
    ax[1].plot([cam["eye"][0], cam["target"][0]], [cam["eye"][1], cam["target"][1]], "k:")
    ax[2].plot(cam["eye"][1], cam["eye"][2], "k^", ms=10); ax[2].axhline(0, color="#3a7bd5")
    ax[2].plot([cam["eye"][1], cam["target"][1]], [cam["eye"][2], cam["target"][2]], "k:")
    for a_, t in ((ax[1], "俯视（米）：实线 = 深度实测，虚线 = 按比例换算"), (ax[2], "侧视（米）：y 远离相机，z 高出水面")):
        a_.set_title(t); a_.set_aspect("equal"); a_.autoscale(); a_.grid(alpha=.3)
    plt.tight_layout(); plt.savefig(out); plt.close(fig)


if __name__ == "__main__":
    main()
