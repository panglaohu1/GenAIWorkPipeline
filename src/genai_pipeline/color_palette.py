"""配色环节：从目标图按区域测颜色 → 调色板；组装出图后按同一批区域比颜色差（验收）。

在 envs/depth 里运行（要 numpy）。区域来自深度定位：每个物件的代表掩码、地形（按法线分崖壁 / 顶面）、水面、天空。
不靠 LLM 判断颜色，也不套固定色阶：
- 每个区域给 主色、受光色（亮度 80 分位附近）、背光色（20 分位附近），本色与光照分开；
- 每个区域再给 2–3 个主要色团（clusters，k-means），可分给花头 / 叶子等不同部件；同时保留 swatches 兼容旧下游；
- 细高物件（高 ≥ 2.2 × 宽）给自上而下的色带（灯塔条纹）；
- 崖壁看色相离散度：单色（离散度小）→ 地形只做明暗，不加 Oskar 的四色竖向色阶。
验收：同机位渲染图与目标图在每个区域的平均颜色差 ΔE（CIE76，Lab），超过阈值的区域判为不合格。

  python color_palette.py check TARGET RENDER REGIONS.json OUT.json
"""
from __future__ import annotations

import colorsys
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

DE_PASS = 12.0   # 区域平均色差在这以内算合格（CIE76，约等于「同一种颜色、明暗略有差别」）


def _hex(c) -> str:
    c = np.clip(np.asarray(c, dtype=np.float64), 0, 1)
    return "#%02X%02X%02X" % tuple(int(round(x * 255)) for x in c)


def _lin(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (0..1, …×3) → CIE Lab (D65)。"""
    l = _lin(rgb)
    M = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = l @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def _load(image: str, size=None) -> np.ndarray:
    im = Image.open(image).convert("RGB")
    if size is not None:
        im = im.resize(size, Image.BILINEAR)
    return np.asarray(im, dtype=np.float32) / 255.0


def _mask(path: str, size) -> np.ndarray:
    return np.asarray(Image.open(path).convert("L").resize(size, Image.NEAREST)) > 127


def _kmeans(x: np.ndarray, k: int, iters: int = 15) -> tuple[np.ndarray, np.ndarray]:
    k = max(1, min(k, len(x)))
    lab = to_lab(x)
    cent = lab[np.linspace(0, len(lab) - 1, k).astype(int)].copy()
    for _ in range(iters):
        idx = np.argmin(((lab[:, None] - cent[None]) ** 2).sum(-1), 1)
        for j in range(k):
            if (idx == j).any():
                cent[j] = lab[idx == j].mean(0)
    rgb = np.stack([np.median(x[idx == j], 0) if (idx == j).any() else x[0] for j in range(k)])
    share = np.array([(idx == j).mean() for j in range(k)])
    return rgb, share


def region_colors(px: np.ndarray) -> dict:
    """一块区域的 主色 / 受光色 / 背光色 + 2–3 个主要色团 + 色相离散度。

    clusters：按占比取前 2–3 个色团（花头 / 叶子 / 泥土可分给不同部件）；下游配色优先消费它。
    swatches：兼容旧字段，内容与 clusters 一致。
    花丛等多色区域不能退回单一中位色（会平均成泥色）；主色取占比最大的色团。"""
    lum = px @ np.array([0.2126, 0.7152, 0.0722])
    lo, hi = np.percentile(lum, 20), np.percentile(lum, 80)
    lit = px[lum >= hi]
    shade = px[lum <= lo]
    sample = px[:: max(1, len(px) // 4000)]
    rgb, share = _kmeans(sample, 3)
    order = np.argsort(-share)
    clusters = [{"hex": _hex(rgb[j]), "share": round(float(share[j]), 3)} for j in order if share[j] >= 0.05][:3]
    if len(clusters) < 2:  # 色团不够时放宽阈值，尽量给出 2 个供部件分色
        clusters = [{"hex": _hex(rgb[j]), "share": round(float(share[j]), 3)} for j in order if share[j] > 0][:3]
    swatches = clusters
    hsv = np.array([colorsys.rgb_to_hsv(*c) for c in px[:: max(1, len(px) // 3000)]])
    sat = hsv[:, 1] > 0.15
    if sat.sum() > 20:
        ang = hsv[sat, 0] * 2 * np.pi
        R = np.hypot(np.cos(ang).mean(), np.sin(ang).mean())
        hue_spread = float(np.degrees(np.sqrt(max(0.0, -2 * np.log(max(R, 1e-9))))))
    else:
        hue_spread = 0.0
    # 主色 = 最大色团；多色区域切勿退回中位数（花丛会变成泥色）
    base = clusters[0]["hex"] if clusters else _hex(np.median(px, 0))
    return {"base": base, "median": _hex(np.median(px, 0)), "lit": _hex(np.median(lit, 0)) if len(lit) else _hex(np.median(px, 0)),
            "shade": _hex(np.median(shade, 0)) if len(shade) else _hex(np.median(px, 0)),
            "clusters": clusters, "swatches": swatches, "hue_spread_deg": round(hue_spread, 1), "pixels": int(len(px))}


def bands(img: np.ndarray, m: np.ndarray, n_bins: int = 14, merge_de: float = 10.0) -> list[dict]:
    """细高物件自上而下的色带：每一行段取中位色，相邻相近的合并。返回 [{from, to（0 = 顶，1 = 底）, hex}]。"""
    ys = np.nonzero(m)[0]
    y0, y1 = ys.min(), ys.max() + 1
    edges = np.linspace(y0, y1, n_bins + 1).astype(int)
    rows = []
    for a, b in zip(edges[:-1], edges[1:]):
        sub = m[a:b]
        if sub.sum() < 5:
            continue
        rows.append([(a - y0) / (y1 - y0), (b - y0) / (y1 - y0), np.median(img[a:b][sub], 0)])
    out = []
    for r in rows:
        if out and float(np.linalg.norm(to_lab(out[-1][2]) - to_lab(r[2]))) < merge_de:
            out[-1][1] = r[1]
            out[-1][2] = (out[-1][2] + r[2]) / 2
        else:
            out.append(r)
    return [{"from": round(float(a), 3), "to": round(float(b), 3), "hex": _hex(c)} for a, b, c in out]


def measure(image: str, regions: dict[str, str], spec: dict) -> dict:
    """regions：区域名 → 掩码 PNG（分析分辨率）。返回写进拆分方案的 palette_measured。"""
    first = next(iter(regions.values()))
    size = Image.open(first).size
    img = _load(image, size)
    out: dict = {"assets": {}, "mode": "measured"}
    sizes = {a["id"]: a.get("size_m") or [1, 1, 1] for a in spec.get("assets", [])}
    for name, path in regions.items():
        m = _mask(path, size)
        if m.sum() < 30:
            continue
        rc = region_colors(img[m])
        if name.startswith("asset:"):
            aid = name.split(":", 1)[1]
            w, d, h = sizes.get(aid, [1, 1, 1])
            if h >= 2.2 * max(w, d, 1e-6):
                rc["bands"] = bands(img, m)
            out["assets"][aid] = rc
        else:
            out[name] = rc
    out.update(audit(img, {n: _mask(p, size) for n, p in regions.items()}, out))
    for name in out.get("suspect", {}):   # 量错地方的地形分区不给后面用（调色、验收、布光都会被它带偏）
        out.pop(name, None)
    rock = out.get("terrain_rock") or out.get("terrain")
    if rock:
        # 崖壁本身是单色（色相集中）就只做明暗；本身多色才允许引擎在测得的几种颜色之间变化
        out["terrain_mode"] = "mono" if rock["hue_spread_deg"] < 25 else "multi"
    return out


LAND = ("terrain", "terrain_rock", "terrain_top")


def _hex_hsv(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)


def _land_hue_unreasonable(c: dict) -> str | None:
    """陆地 / 草地主色偏蓝青：多半圈到了海或远水（岛顶草地量成蓝灰）。"""
    hex_ = c.get("base") or c.get("median")
    if not hex_:
        return None
    hue, sat, _ = _hex_hsv(hex_)
    # 色相约 180°–260°（青→蓝），且有一定饱和度
    if sat >= 0.12 and 0.50 <= hue <= 0.72:
        return f"主色 {hex_} 偏蓝青（草地 / 陆地不应如此），区域多半圈错了地方"
    return None


def audit(img: np.ndarray, masks: dict[str, np.ndarray], colors: dict, max_unassigned: float = 0.3) -> dict:
    """取色区域的自检：
    - coverage：画面里有多少像素归到了某个区域（物件 / 地形 / 水 / 天）。没归属的像素取色时等于没看见；
      超过 max_unassigned（默认 30%）说明拆分漏了东西（成片的花、树没被圈到）→ 应报警并重新圈。
    - suspect：地形分区量出来的颜色不合常理（与水面几乎一样，或草地主色偏蓝）——区域圈错，应重新圈。
    返回 {"coverage": {"assigned", "unassigned", "warn"}, "suspect": {区域: 原因}, "needs_retry": bool}。"""
    H, W = img.shape[:2]
    cover = np.zeros((H, W), bool)
    for m in masks.values():
        if m.shape == cover.shape:
            cover |= m
    un = float(1 - cover.mean())
    cov_warn = un > max_unassigned
    res = {"coverage": {"assigned": round(1 - un, 3), "unassigned": round(un, 3),
                        "warn": cov_warn, "threshold": max_unassigned}}
    water = colors.get("water")
    suspect = {}
    if water:
        wl = to_lab(np.array([int(water["median"][i:i + 2], 16) / 255 for i in (1, 3, 5)]))
        for name in LAND:
            c = colors.get(name)
            if not c:
                continue
            cl = to_lab(np.array([int(c["median"][i:i + 2], 16) / 255 for i in (1, 3, 5)]))
            de = float(np.linalg.norm(cl - wl))
            if de < 12:
                suspect[name] = f"颜色 {c['median']} 与水面 {water['median']} 几乎相同（ΔE {de:.1f}），区域多半圈到了水"
    for name in LAND:
        c = colors.get(name)
        if not c or name in suspect:
            continue
        why = _land_hue_unreasonable(c)
        if why:
            suspect[name] = why
    if suspect:
        res["suspect"] = suspect
    res["needs_retry"] = bool(cov_warn or suspect)
    return res


def needs_retry(pm: dict) -> list[str]:
    """拆分自检是否该触发重新圈：返回原因列表（空 = 不用重试）。
    触发条件：未归属像素超过约 30%，或某个区域颜色不合常理（草地偏蓝 / 与水面同色）。"""
    if not isinstance(pm, dict) or pm.get("error"):
        return []
    reasons = []
    cov = pm.get("coverage") or {}
    if cov.get("warn"):
        thr = float(cov.get("threshold") or 0.3)
        reasons.append(f"未归属像素 {float(cov.get('unassigned') or 0) * 100:.0f}%（阈值 {thr * 100:.0f}%）")
    for name, why in (pm.get("suspect") or {}).items():
        reasons.append(f"{name}：{why}")
    if pm.get("needs_retry") and not reasons:
        reasons.append("拆分自检未通过")
    return reasons


def light_direction(image: str, npz, mask: np.ndarray, frame: dict) -> dict | None:
    """从目标图估主光方向（本机测量，不靠猜）：单色地块上亮度 ≈ 环境 + 法线·光向（朗伯），对法线做最小二乘。
    mask 与 npz 法线同为分析分辨率；frame 是深度定位的世界轴（相机坐标里的 right / fwd / up）。
    返回 {"dir_world": 指向光源的单位向量（世界系 x 右、y 远离相机、z 上）, "ambient", "key", "fit_r2"}。"""
    D = np.load(npz)
    if "normal" not in D.files:
        return None
    step = max(1, max(D["points"].shape[:2]) // 512)
    N = D["normal"][::step, ::step].astype(np.float64)
    H, W = N.shape[:2]
    if mask.shape != (H, W):
        return None
    img = _load(image, (W, H))
    lum = img @ np.array([0.2126, 0.7152, 0.0722])
    m = mask & np.isfinite(N).all(-1)
    if m.sum() < 500:
        return None
    n = N[m] / (np.linalg.norm(N[m], axis=1, keepdims=True) + 1e-9)
    y = lum[m]
    # 只用一种颜色的像素（地块主色附近）：不同颜色的面亮度差来自本色，不是光照
    lab = to_lab(img[m])
    ref = np.median(lab[:, 1:], 0)
    same = np.linalg.norm(lab[:, 1:] - ref, axis=1) < 12
    if same.sum() < 300:
        same = np.ones(len(y), bool)
    A = np.c_[np.ones(same.sum()), n[same]]
    coef, *_ = np.linalg.lstsq(A, y[same], rcond=None)
    pred = A @ coef
    r2 = 1 - ((y[same] - pred) ** 2).sum() / max(((y[same] - y[same].mean()) ** 2).sum(), 1e-9)
    v = coef[1:]
    if np.linalg.norm(v) < 1e-6:
        return None
    lc = v / np.linalg.norm(v)                          # 相机坐标里指向光源的方向
    R = np.array([frame["right"], frame["fwd"], frame["up"]])
    lw = R @ lc
    lw /= np.linalg.norm(lw)
    return {"dir_world": [round(float(x), 4) for x in lw], "ambient": round(float(coef[0]), 3),
            "key": round(float(np.linalg.norm(v)), 3), "fit_r2": round(float(r2), 3)}


def _dominant_median(px):
    """一组像素（N×3，0–1）里占多数的那一簇颜色的中位数：两簇 k-means（按亮度初始化），取像素多的一簇。
    木板之间的暗缝、物件边上的描边不会把中位色拉偏。"""
    px = np.asarray(px, dtype=np.float64).reshape(-1, 3)
    if len(px) < 20:
        return np.median(px, 0)
    lum = px @ np.array([0.2126, 0.7152, 0.0722])
    c = np.array([px[lum <= np.percentile(lum, 25)].mean(0), px[lum >= np.percentile(lum, 75)].mean(0)])
    for _ in range(8):
        lab = np.argmin(((px[:, None] - c[None]) ** 2).sum(-1), 1)
        for j in range(2):
            if (lab == j).any():
                c[j] = px[lab == j].mean(0)
    big = int(np.bincount(lab, minlength=2).argmax())
    return np.median(px[lab == big], 0)


def check(target: str, render: str, regions: dict[str, str], own: dict | None = None) -> dict:
    """同机位验收：每个区域里，渲染图与目标图的平均颜色差（ΔE）。
    own = {"asset:<id>": [r,g,b]}（sRGB 0–1）：该物件在渲染图里自己的像素中位色（按材质 ID 图取）——有了它，
    物件的颜色就不会因为摆放略偏、区域里混进了水面而被误判；目标侧的物件区域也去掉和水面同色的像素（框选的区域常带进一片水）。"""
    first = next(iter(regions.values()))
    size = Image.open(first).size
    a, b = _load(target, size), _load(render, size)
    own = own or {}
    water_lab = None
    if "water" in regions:
        wm = _mask(regions["water"], size)
        if wm.sum() >= 30:
            water_lab = to_lab(np.median(a[wm], 0))
    res = {}
    for name, path in regions.items():
        m = _mask(path, size)
        if m.sum() < 30:
            continue
        mt = m
        if name.startswith("asset:") and water_lab is not None:
            keep = m & (np.linalg.norm(to_lab(a) - water_lab, axis=-1) > 12)
            if keep.sum() >= 30:
                mt = keep
        # 物件：取占多数的那簇颜色（木板缝、描边、阴影不拉偏）；渲染侧的 own 也按同样方式取
        tc = _dominant_median(a[mt]) if (name in own or name.startswith("asset:")) else np.median(a[mt], 0)
        rc = np.asarray(own[name], dtype=np.float64) if name in own else np.median(b[m], 0)
        la, lb = to_lab(tc), to_lab(rc)
        de = float(np.linalg.norm(la - lb))
        res[name] = {"delta_e": round(de, 1), "target": _hex(tc), "render": _hex(rc),
                     "pass": de <= DE_PASS, **({"own": True} if name in own else {})}
    # 曝光：实体区域（物件、地形）里目标图与渲染图的线性亮度之比（中位数）。色相对了但整体偏暗 / 偏亮时，
    # 这个比值告诉渲染该加减几档曝光（log2）
    ratios = []
    for name, path in regions.items():
        if name in ("sky", "water") or name not in res:
            continue
        m = _mask(path, size)
        la = float(np.median(_lin(a[m]) @ np.array([0.2126, 0.7152, 0.0722])))
        lb = float(np.median(_lin(b[m]) @ np.array([0.2126, 0.7152, 0.0722])))
        if la > 1e-3 and lb > 1e-3:
            ratios.append(la / lb)
    exposure = round(float(np.log2(np.median(ratios))), 3) if ratios else 0.0
    weights = {k: (3 if k.startswith("terrain") else 2 if k == "water" else 1) for k in res}
    tot = sum(weights.values()) or 1
    score = sum(min(1.0, res[k]["delta_e"] / 40) * w for k, w in weights.items()) / tot
    return {"regions": res, "pass": all(v["pass"] for v in res.values()), "mean_delta_e": round(
        sum(v["delta_e"] for v in res.values()) / max(1, len(res)), 1), "color_score": round(10 * (1 - score), 1),
            "threshold": DE_PASS, "exposure_stops": exposure}


def lambert_fit(img: np.ndarray, normals: np.ndarray, mask: np.ndarray, lc=None) -> dict | None:
    """布光的实测（与几何无关）：单色像素上，线性颜色 ≈ a + b·max(0, 法线·光向)（逐通道最小二乘）。
    b / a = 主光相对环境光的强度与颜色比——目标图用深度模型的法线、渲染图用渲染出的法线各算一次，比的是同一个量，
    不要求两边的面一一对应（崖壁分块不同也能比）。normals：相机坐标（OpenCV：x 右、y 下、z 向前）。lc 不给时先按亮度拟合光向。"""
    m = mask & np.isfinite(normals).all(-1) & (np.linalg.norm(normals, axis=-1) > 0.5)
    m &= (img.max(-1) < 0.96)   # 过曝像素不线性（顶到 1 会把亮面压扁、亮暗比偏小），不参与拟合
    if m.sum() < 300:
        return None
    px = img[m]
    lab = to_lab(px)
    same = np.linalg.norm(lab[:, 1:] - np.median(lab[:, 1:], 0), axis=1) < 12
    if same.sum() < 200:
        same = np.ones(len(px), bool)
    n = normals[m][same]
    n = n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-9)
    lin = _lin(px[same])
    if lc is None:
        y = px[same] @ np.array([0.2126, 0.7152, 0.0722])
        coef, *_ = np.linalg.lstsq(np.c_[np.ones(len(y)), n], y, rcond=None)
        v = coef[1:]
        if np.linalg.norm(v) < 1e-6:
            return None
        lc = v / np.linalg.norm(v)
    lc = np.asarray(lc, np.float64)
    cosv = np.clip(n @ lc, 0, None)
    if np.ptp(cosv) < 0.05:
        return None
    A = np.c_[np.ones(len(cosv)), cosv]
    a, b = [], []
    for c in range(3):
        co, *_ = np.linalg.lstsq(A, lin[:, c], rcond=None)
        a.append(float(co[0]))
        b.append(float(co[1]))
    k = [round(max(0.0, bb) / max(aa, 1e-3), 4) for aa, bb in zip(a, b)]
    return {"lc": [round(float(x), 5) for x in lc], "a": [round(x, 5) for x in a], "b": [round(x, 5) for x in b],
            "k": k, "pixels": int(len(cosv)), "cos_range": [round(float(cosv.min()), 3), round(float(cosv.max()), 3)]}


def light_target(image: str, npz, mask_path: str) -> dict | None:
    """目标图：深度模型的法线（分析分辨率）+ 地块掩码 → lambert_fit。"""
    D = np.load(npz)
    if "normal" not in D.files:
        return None
    step = max(1, max(D["points"].shape[:2]) // 512)
    N = D["normal"][::step, ::step].astype(np.float64)
    H, W = N.shape[:2]
    return lambert_fit(_load(image, (W, H)), N, _mask(mask_path, (W, H)))


def light_render(render: str, normal_png: str, mask_path: str, lc) -> dict | None:
    """渲染图：法线通道图（相机坐标 OpenCV，n·0.5+0.5 存成 RGB，透明处无几何）+ 同一块掩码 → lambert_fit（光向用目标图的）。"""
    im = Image.open(normal_png).convert("RGBA")
    W, H = im.size
    arr = np.asarray(im, np.float32) / 255.0
    N = arr[..., :3] * 2 - 1
    N[arr[..., 3] < 0.5] = np.nan
    return lambert_fit(_load(render, (W, H)), N, _mask(mask_path, (W, H)), lc)


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "check":
        regs = json.loads(Path(sys.argv[4]).read_text())
        own = json.loads(Path(sys.argv[6]).read_text()) if len(sys.argv) >= 7 else None
        Path(sys.argv[5]).write_text(json.dumps(check(sys.argv[2], sys.argv[3], regs, own), ensure_ascii=False, indent=1))
    elif len(sys.argv) >= 6 and sys.argv[1] == "lighttarget":
        # lighttarget 目标图 npz 掩码 输出.json
        Path(sys.argv[5]).write_text(json.dumps(light_target(sys.argv[2], sys.argv[3], sys.argv[4]), ensure_ascii=False))
    elif len(sys.argv) >= 7 and sys.argv[1] == "lightrender":
        # lightrender 渲染图 法线图 掩码 光向json 输出.json
        lc = json.loads(Path(sys.argv[5]).read_text())
        Path(sys.argv[6]).write_text(json.dumps(light_render(sys.argv[2], sys.argv[3], sys.argv[4], lc), ensure_ascii=False))
    else:
        print(__doc__)
        sys.exit(2)
