# SPDX-License-Identifier: GPL-3.0-or-later
"""环境与材质生成（本机，numpy）：天空盒、HDR 环境、PBR 贴图。GenAIWorkPipeline 与 Scenario 本地 fork 共用同一份。

- 天空盒（实测）：把目标图里天空 / 水面每一行的颜色，按相机换算成仰角，铺成 2:1 等距柱状全景（方位均匀）。
  画面里看得到的天和海就是目标图的颜色，看不到的部分按最近的实测值外延。
- 天空盒（生成）：FLUX 出的 2:1 全景图，修左右接缝、两极收拢。
- HDR：LDR 全景 → 线性 HDR（高光段按膝点展开，太阳 / 亮云更亮），写 Radiance .hdr（纯 numpy）或 EXR（在 Blender 里）。
- PBR：底色图 → 可平铺（错位交叉淡化）→ 高度 / 法线（OpenGL，Y 向上）/ 粗糙度 / AO / 金属度，全部周期卷积，贴图本身无缝。

图片读写优先用 PIL；没有 PIL（在 Blender 里）就用 bpy。数组一律 float32、0–1，sRGB 图标注 srgb，线性的标注 lin。
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

LUM = np.array([0.2126, 0.7152, 0.0722], np.float32)


# ───────────────────────── 颜色与读写 ─────────────────────────
def s2l(c):
    c = np.clip(np.asarray(c, np.float32), 0, 1)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def l2s(c):
    c = np.clip(np.asarray(c, np.float32), 0, None)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(np.minimum(c, 1.0), 1 / 2.4) - 0.055).astype(np.float32)


def load_rgb(path: str) -> np.ndarray:
    """→ H×W×3 float32，0–1，按文件本来的编码（PNG/JPEG 即 sRGB）。"""
    try:
        from PIL import Image
        return np.asarray(Image.open(path).convert("RGB"), np.float32) / 255.0
    except ImportError:
        import bpy
        im = bpy.data.images.load(path, check_existing=False)
        try:
            w, h = im.size
            a = np.array(im.pixels[:], np.float32).reshape(h, w, 4)[::-1, :, :3]
        finally:
            bpy.data.images.remove(im)
        return a.copy()


def load_mask(path: str, size_wh) -> np.ndarray:
    try:
        from PIL import Image
        return np.asarray(Image.open(path).convert("L").resize(size_wh, Image.NEAREST)) > 127
    except ImportError:
        import bpy
        im = bpy.data.images.load(path, check_existing=False)
        try:
            im.scale(*size_wh)
            w, h = size_wh
            return (np.array(im.pixels[:], np.float32).reshape(h, w, 4)[::-1, :, 0] > 0.5).copy()
        finally:
            bpy.data.images.remove(im)


def save_rgb(path: str, arr: np.ndarray) -> str:
    """sRGB 0–1 → 8 位 PNG（加一点抖动，渐变天空不出色阶）。"""
    a = np.clip(np.asarray(arr, np.float32), 0, 1)
    rng = np.random.default_rng(0)
    q = np.clip(np.round(a * 255 + rng.uniform(-0.5, 0.5, a.shape)), 0, 255).astype(np.uint8)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    try:
        from PIL import Image
        Image.fromarray(q if q.ndim == 3 else q).save(path)
    except ImportError:
        import bpy
        h, w = q.shape[:2]
        im = bpy.data.images.new(os.path.basename(path), w, h, alpha=False)
        rgba = np.ones((h, w, 4), np.float32)
        rgba[..., :3] = (q if q.ndim == 3 else q[..., None]).astype(np.float32) / 255.0
        im.pixels.foreach_set(rgba[::-1].reshape(-1))
        im.filepath_raw = path
        im.file_format = "PNG"
        im.save()
        bpy.data.images.remove(im)
    return path


def save_hdr(path: str, lin: np.ndarray) -> str:
    """线性 RGB → Radiance .hdr（RGBE，未压缩扫描线），Blender / Godot 都能读。"""
    a = np.maximum(np.asarray(lin, np.float32), 0)
    h, w = a.shape[:2]
    m = a.max(-1)
    e = np.where(m > 1e-32, np.floor(np.log2(np.maximum(m, 1e-32))) + 1, 0)
    scale = np.where(m > 1e-32, 256.0 / np.exp2(e), 0)
    rgbe = np.zeros((h, w, 4), np.uint8)
    rgbe[..., :3] = np.clip(a * scale[..., None], 0, 255).astype(np.uint8)
    rgbe[..., 3] = np.where(m > 1e-32, np.clip(e + 128, 0, 255), 0).astype(np.uint8)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n" + f"-Y {h} +X {w}\n".encode())
        f.write(rgbe.tobytes())
    return path


# ───────────────────────── 周期卷积（贴图无缝的关键） ─────────────────────────
def blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """周期边界的高斯模糊（FFT）：左右、上下当作首尾相接，结果天然可平铺。"""
    if sigma <= 0:
        return a.astype(np.float32)
    h, w = a.shape[:2]
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]
    g = np.exp(-2 * (math.pi ** 2) * (sigma ** 2) * (fx ** 2 + fy ** 2)).astype(np.float32)
    if a.ndim == 2:
        return np.real(np.fft.ifft2(np.fft.fft2(a) * g)).astype(np.float32)
    return np.stack([np.real(np.fft.ifft2(np.fft.fft2(a[..., c]) * g)) for c in range(a.shape[2])], -1).astype(np.float32)


# ───────────────────────── 天空盒（实测） ─────────────────────────
def row_angles(height: int, cam: dict) -> np.ndarray:
    """目标图每一行中心的仰角（度，水平为 0，向上为正）：针孔相机，水平视场 fov_deg、宽高比 aspect、俯角 pitch_down_deg。"""
    hfov = math.radians(float(cam.get("fov_deg", 50)))
    aspect = float(cam.get("aspect", 1.0))
    tv = math.tan(hfov / 2) / max(aspect, 1e-6)
    y = (np.arange(height) + 0.5) / height
    v = np.degrees(np.arctan((0.5 - y) * 2 * tv))
    return v - float(cam.get("pitch_down_deg", 0.0))


def profile(img: np.ndarray, mask: np.ndarray, angles: np.ndarray, bin_deg: float = 0.5, min_px: int = 8) -> list:
    """区域每一行的中位色 → 按仰角分箱：[(仰角, [r,g,b] sRGB)]，从低到高。"""
    rows = {}
    for y in np.nonzero(mask.any(1))[0]:
        m = mask[y]
        if m.sum() < min_px:
            continue
        k = round(float(angles[y]) / bin_deg) * bin_deg
        rows.setdefault(k, []).append(np.median(img[y][m], 0))
    prof = [(k, np.median(np.array(v), 0)) for k, v in sorted(rows.items())]
    if len(prof) >= 5:   # 相邻仰角做滑动中位：水面倒影、浪花这类零星杂色不会在全景里拉成色带
        cs = np.array([c for _, c in prof])
        sm = [np.median(cs[max(0, i - 3):i + 4], 0) for i in range(len(cs))]
        prof = [(k, c) for (k, _), c in zip(prof, sm)]
    return [(k, [float(x) for x in c]) for k, c in prof]


def _interp(prof: list, e: np.ndarray) -> np.ndarray:
    ks = np.array([p[0] for p in prof], np.float32)
    cs = s2l(np.array([p[1] for p in prof], np.float32))   # 在线性空间插值，渐变不发灰
    out = np.stack([np.interp(e, ks, cs[:, c]) for c in range(3)], -1)
    return out.astype(np.float32)


def sky_dome(sky: list, sea: list, width: int = 2048, height: int = 1024, horizon_soft_deg: float = 1.0) -> np.ndarray:
    """方位均匀的等距柱状天空盒（线性 RGB）：仰角 > 0 取天空剖面、< 0 取水面剖面，地平线处按 horizon_soft_deg 柔接。
    剖面之外按最近的实测值延伸（天顶 / 正下方各自保持最后一个颜色）。"""
    e = 90.0 - (np.arange(height) + 0.5) / height * 180.0          # 每行的仰角（度）
    if not sky and not sea:
        raise ValueError("没有天空也没有水面的实测颜色")
    sky_c = _interp(sky, e) if sky else None
    sea_c = _interp(sea, e) if sea else None
    if sky_c is None:
        col = sea_c
    elif sea_c is None:
        col = sky_c
    else:
        t = np.clip((e / max(horizon_soft_deg, 1e-3) + 1) / 2, 0, 1)[:, None]
        col = sea_c * (1 - t) + sky_c * t
    return np.repeat(col[:, None, :], width, axis=1)


def measured_dome(target: str, regions: dict, cam: dict, out_png: str, out_hdr: str = "",
                  width: int = 2048, height: int = 1024) -> dict:
    """目标图 + 天空 / 水面掩码 + 相机 → 天空盒 PNG（sRGB）与 .hdr（线性）。返回剖面摘要。"""
    img = load_rgb(target)
    h, w = img.shape[:2]
    ang = row_angles(h, cam)
    sky_raw = profile(img, load_mask(regions["sky"], (w, h)), ang) if regions.get("sky") else []
    sea_raw = profile(img, load_mask(regions["water"], (w, h)), ang) if regions.get("water") else []
    sky = [p for p in sky_raw if p[0] > -2]       # 天空掩码偶尔吃进地平线下几行：只要地平线以上
    sea = [p for p in sea_raw if p[0] < 2]
    # 相机大俯角时整幅画面都在几何地平线以下，过滤会把天空剖面清空；此时把天空仰角重映射到海面之上，保留色带。
    if not sky and sky_raw:
        sea_top = max((p[0] for p in sea), default=0.0)
        lo, hi = sky_raw[0][0], sky_raw[-1][0]
        span = max(hi - lo, 1e-3)
        target_lo = max(sea_top, 0.0) + 0.5
        sky = [(target_lo + (p[0] - lo) / span * max(span, 8.0), p[1]) for p in sky_raw]
    if not sea and sea_raw:
        sky_bot = min((p[0] for p in sky), default=0.0)
        lo, hi = sea_raw[0][0], sea_raw[-1][0]
        span = max(hi - lo, 1e-3)
        target_hi = min(sky_bot, 0.0) - 0.5
        sea = [(target_hi - (hi - p[0]) / span * max(span, 8.0), p[1]) for p in sea_raw]
    lin = sky_dome(sky, sea, width, height)
    save_rgb(out_png, l2s(lin))
    if out_hdr:
        save_hdr(out_hdr, lin)
    return {"png": out_png, "hdr": out_hdr or None, "sky_rows": len(sky), "sea_rows": len(sea),
            "sky_range_deg": [sky[0][0], sky[-1][0]] if sky else None,
            "sea_range_deg": [sea[0][0], sea[-1][0]] if sea else None}


# ───────────────────────── 天空盒（生成图修整） ─────────────────────────
def seam_wrap(img: np.ndarray, band: float = 0.06) -> np.ndarray:
    """2:1 全景左右接缝：左右两端每行的颜色差，在两侧 band 宽度内线性摊开，让首尾两列在中间值相接。"""
    h, w = img.shape[:2]
    b = max(2, int(w * band))
    d = (img[:, :1] - img[:, -1:]) / 2                    # 左端比右端高多少（每行）
    out = img.astype(np.float32).copy()
    t = (1 - np.arange(b) / (b - 1))[None, :, None].astype(np.float32)   # 边缘 1 → 内侧 0
    out[:, :b] -= d * t
    out[:, w - b:] += d * t[:, ::-1]
    return out


def pole_fix(img: np.ndarray, band: float = 0.08) -> np.ndarray:
    """等距柱状图的两极：靠近顶 / 底的行逐渐收拢成该行的平均色，避免天顶出现放射状拉丝。"""
    h = img.shape[0]
    b = max(2, int(h * band))
    out = img.copy()
    for y in range(b):
        t = (1 - y / b) ** 2
        out[y] = out[y] * (1 - t) + out[y].mean(0) * t
        out[h - 1 - y] = out[h - 1 - y] * (1 - t) + out[h - 1 - y].mean(0) * t
    return out


def ldr_to_hdr(srgb: np.ndarray, knee: float = 0.8, boost: float = 8.0) -> np.ndarray:
    """LDR → 线性 HDR：亮度超过膝点的部分按平方曲线放大到最多 boost 倍（太阳、亮云能照亮场景），其余保持线性。"""
    lin = s2l(srgb)
    lum = lin @ LUM
    k = float(s2l(np.float32(knee)))
    t = np.clip((lum - k) / max(1 - k, 1e-6), 0, 1)
    return (lin * (1 + (boost - 1) * t ** 2)[..., None]).astype(np.float32)


# ───────────────────────── PBR 贴图 ─────────────────────────
def tileable(img: np.ndarray, band: float = 0.18) -> np.ndarray:
    """错位交叉淡化：原图与「平移半幅」的自身按到边缘的距离混合，四边首尾相接，中心保持原图。"""
    h, w = img.shape[:2]
    shifted = np.roll(np.roll(img, h // 2, 0), w // 2, 1)
    y = np.minimum(np.arange(h), h - 1 - np.arange(h)) / (h * band)
    x = np.minimum(np.arange(w), w - 1 - np.arange(w)) / (w * band)
    m = np.clip(np.minimum(y[:, None], x[None, :]), 0, 1)
    m = m * m * (3 - 2 * m)
    if img.ndim == 3:
        m = m[..., None]
    return (img * m + shifted * (1 - m)).astype(np.float32)


def pbr_maps(albedo_srgb: np.ndarray, depth: float = 1.0, roughness: float = 0.75) -> dict:
    """可平铺底色图 → {albedo, height, normal, roughness, ao, metallic}（都是 0–1 数组；法线为 OpenGL 约定 RGB）。
    高度：去掉低频光照后的亮度（细节越暗越凹）；法线：高度的周期梯度；AO：高度相对邻域的凹陷；
    粗糙度：基值 ± 高度起伏（凸起处被磨得更光）。"""
    a = np.clip(albedo_srgb, 0, 1).astype(np.float32)
    h, w = a.shape[:2]
    lum = s2l(a) @ LUM
    base = blur(lum, max(h, w) / 16)
    hp = lum - base                                   # 去掉大块明暗（烘进图里的光照）
    hgt = blur(hp, 1.0)
    span = np.percentile(hgt, 99) - np.percentile(hgt, 1)
    hgt = np.clip((hgt - np.percentile(hgt, 1)) / max(span, 1e-6), 0, 1)
    k = depth * max(h, w) / 256.0
    gx = (np.roll(hgt, -1, 1) - np.roll(hgt, 1, 1)) * 0.5 * k
    gy = (np.roll(hgt, -1, 0) - np.roll(hgt, 1, 0)) * 0.5 * k
    n = np.stack([-gx, gy, np.ones_like(hgt)], -1)    # OpenGL：图像 y 向下，切线空间 Y 向上
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    normal = n * 0.5 + 0.5
    cav = np.clip((blur(hgt, max(h, w) / 64) - hgt) * 3.0, 0, 1)
    ao = np.clip(1 - cav, 0, 1)
    rough = np.clip(roughness + (0.5 - hgt) * 0.25, 0.05, 1)
    return {"albedo": a, "height": hgt, "normal": normal.astype(np.float32), "roughness": rough.astype(np.float32),
            "ao": ao.astype(np.float32), "metallic": np.zeros_like(hgt)}


def save_maps(maps: dict, out_dir: str, stem: str = "material") -> dict:
    """贴图写成 PNG：albedo 为 sRGB 彩色，其余为线性数据（灰度图存三通道，法线存 RGB）。"""
    out = {}
    for k, v in maps.items():
        arr = v if v.ndim == 3 else np.repeat(v[..., None], 3, -1)
        out[k] = save_rgb(os.path.join(out_dir, f"{stem}_{k}.png"), arr)
    return out


# ───────────────────────── 命令行（流水线在 envs/depth 子进程里调用） ─────────────────────────
def _main(argv: list) -> int:
    if len(argv) >= 2 and argv[1] == "dome":
        req = json.loads(open(argv[2]).read())
        res = measured_dome(req["target"], req["regions"], req["camera"], req["out_png"], req.get("out_hdr", ""),
                            int(req.get("width", 2048)), int(req.get("height", 1024)))
        open(argv[3], "w").write(json.dumps(res, ensure_ascii=False))
        return 0
    if len(argv) >= 2 and argv[1] == "pbr":
        req = json.loads(open(argv[2]).read())
        img = load_rgb(req["image"])
        side = min(img.shape[:2])
        img = img[:side, :side]
        maps = pbr_maps(tileable(img), float(req.get("depth", 1.0)), float(req.get("roughness", 0.75)))
        res = save_maps(maps, req["out_dir"], req.get("stem", "material"))
        open(argv[3], "w").write(json.dumps(res, ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
