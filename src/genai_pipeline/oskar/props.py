"""Oskar 引擎的物件构件（纯 Python，确定性）：塔 / 灯塔、房屋（含风车叶片）、庄园（方塔+石屋+拱门）、码头、小船。

和地形一样只出「部件 → 面」的网格（Mesh.as_dict），由 Blender 侧统一上材质、圆角、描边，所以物件与岛体同一种画风。
尺寸一律用米（深度定位实测的 size_m），颜色一律用配色环节实测的色值；结构上的选择（几层色带、什么屋顶、
码头是直的还是 L 形）由本机 LLM 看裁剪图决定，这里只负责把参数做成几何。原点在底面中心，z 朝上，-y 为正面。

spec = {"type": "tower" | "house" | "pier" | "boat" | "tree" | "estate" | "flower", "size_m": [宽, 深, 高], "colors": {角色: "#rrggbb"}, ...}
"""
from __future__ import annotations

import math

from .build import Mesh

TYPES = ("tower", "house", "pier", "boat", "tree", "estate", "flower")


def _ring(n, r, z, phase=0.0, cx=0.0, cy=0.0, sx=1.0, sy=1.0):
    return [(cx + r * sx * math.cos(phase + 2 * math.pi * i / n), cy + r * sy * math.sin(phase + 2 * math.pi * i / n), z)
            for i in range(n)]


def _frustum(mesh, role, n, r0, r1, z0, z1, cap_bottom=False, cap_top=False, cx=0.0, cy=0.0, top_role=None):
    a, b = _ring(n, r0, z0, cx=cx, cy=cy), _ring(n, r1, z1, cx=cx, cy=cy)
    for i in range(n):
        j = (i + 1) % n
        mid = ((a[i][0] + a[j][0]) / 2 - cx, (a[i][1] + a[j][1]) / 2 - cy, 0)
        mesh.add(role, [a[i], a[j], b[j], b[i]], mid)
    if cap_bottom:
        mesh.add(role, a[::-1], (0, 0, -1))
    if cap_top:
        mesh.add(top_role or role, b, (0, 0, 1))


def _cone(mesh, role, n, r, z0, h, cx=0.0, cy=0.0):
    a = _ring(n, r, z0, cx=cx, cy=cy)
    tip = (cx, cy, z0 + h)
    for i in range(n):
        j = (i + 1) % n
        mesh.add(role, [a[i], a[j], tip], ((a[i][0] + a[j][0]) / 2 - cx, (a[i][1] + a[j][1]) / 2 - cy, 0.3))
    mesh.add(role, a[::-1], (0, 0, -1))


def _dome(mesh, role, n, r, z0, h, rings=3, cx=0.0, cy=0.0):
    prev = _ring(n, r, z0, cx=cx, cy=cy)
    for k in range(1, rings + 1):
        t = k / rings
        rr, zz = r * math.cos(t * math.pi / 2), z0 + h * math.sin(t * math.pi / 2)
        if k == rings:
            tip = (cx, cy, zz)
            for i in range(n):
                j = (i + 1) % n
                mesh.add(role, [prev[i], prev[j], tip], (prev[i][0] - cx, prev[i][1] - cy, 0.5))
        else:
            cur = _ring(n, rr, zz, cx=cx, cy=cy)
            for i in range(n):
                j = (i + 1) % n
                mesh.add(role, [prev[i], prev[j], cur[j], cur[i]], (prev[i][0] - cx, prev[i][1] - cy, 0.3))
            prev = cur
    mesh.add(role, _ring(n, r, z0, cx=cx, cy=cy)[::-1], (0, 0, -1))


def _box(mesh, role, x0, x1, y0, y1, z0, z1, top_role=None, skip_bottom=True):
    c = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    mesh.add(role, [c[0], c[1], c[5], c[4]], (0, -1, 0))
    mesh.add(role, [c[1], c[2], c[6], c[5]], (1, 0, 0))
    mesh.add(role, [c[2], c[3], c[7], c[6]], (0, 1, 0))
    mesh.add(role, [c[3], c[0], c[4], c[7]], (-1, 0, 0))
    mesh.add(top_role or role, [c[4], c[5], c[6], c[7]], (0, 0, 1))
    if not skip_bottom:
        mesh.add(role, [c[0], c[3], c[2], c[1]], (0, 0, -1))


# ───────────────────────── 塔 / 灯塔 ─────────────────────────

def tower(spec: dict) -> Mesh:
    """锥台塔身（按实测色带分段上色）+ 可选瞭望平台、灯室、顶盖。
    bands：自上而下 [{"from": 0..1, "to": 0..1, "hex"}]（配色环节对细高物件测的色带，0 = 顶）。
    body / lantern / cap：塔身、灯室、顶盖各占总高的比例（LLM 看图给，缺省 0.72 / 0.14 / 0.14）。"""
    w, _, h = (float(x) for x in spec["size_m"])
    m = Mesh()
    n = int(spec.get("sides", 10))
    fr = spec.get("fractions") or {}
    body_f = float(fr.get("body", 0.72))
    lant_f = float(fr.get("lantern", 0.14)) if spec.get("lantern", True) else 0.0
    cap_f = max(0.0, 1.0 - body_f - lant_f)
    taper = max(0.5, min(1.0, float(spec.get("taper", 0.78))))   # 顶半径 / 底半径
    r0 = w / 2 * 0.92
    r1 = r0 * taper
    zb = h * body_f
    bands = spec.get("bands") or []
    # 色带是对整个物件自上而下测的（0 = 顶）：顶上 1-body 那一段是灯室、顶盖，塔身只取下面 body 那一段，
    # 换算成塔身内自下而上的 0..1（否则灯室的深色会在塔身顶上再出现一遍）
    cuts = []
    top_cut = 1.0 - body_f if (lant_f > 0 or cap_f > 0) else 0.0
    for k, b in enumerate(bands):
        f0, f1 = max(float(b["from"]), top_cut), float(b["to"])
        if f1 - f0 < 1e-3:
            continue
        t0, t1 = (1 - f1) / (1 - top_cut), (1 - f0) / (1 - top_cut)
        cuts.append((max(0.0, t0), min(1.0, t1), f"band{k}"))   # 塔身内自下而上的 0..1
    cuts.sort()
    if not cuts:
        cuts = [(0.0, 1.0, "body")]
    cuts[0] = (0.0, cuts[0][1], cuts[0][2])
    cuts[-1] = (cuts[-1][0], 1.0, cuts[-1][2])
    for t0, t1, role in cuts:
        if t1 - t0 < 1e-3:
            continue
        z0, z1 = zb * t0, zb * t1
        _frustum(m, role, n, r0 + (r1 - r0) * t0, r0 + (r1 - r0) * t1, z0, z1, cap_bottom=t0 == 0.0)
    top_z = zb
    if spec.get("gallery", True):   # 瞭望平台：外挑一圈薄板 + 一圈矮栏
        g_r = r1 * 1.32
        _frustum(m, "trim", n, g_r, g_r, top_z, top_z + h * 0.018, cap_bottom=True, cap_top=True)
        rail_h = h * 0.035
        _frustum(m, "trim", n, g_r * 0.98, g_r * 0.98, top_z + h * 0.018, top_z + h * 0.018 + rail_h)
        top_z += h * 0.018
    if lant_f > 0:   # 灯室：比塔顶细一圈的玻璃筒
        lr = r1 * 0.78
        _frustum(m, "window", n, lr, lr, top_z, top_z + h * lant_f, cap_bottom=True)
        top_z += h * lant_f
    cap_h = max(h - top_z, h * 0.05) if cap_f > 0 else 0
    if cap_h > 0:
        cr = (r1 * 0.95) if lant_f > 0 else r1 * 1.05
        if spec.get("cap", "dome") == "cone":
            _cone(m, "roof", n, cr, top_z, cap_h)
        else:
            _dome(m, "roof", n, cr, top_z, cap_h * 0.75)
            _cone(m, "roof", 6, cr * 0.12, top_z + cap_h * 0.75, cap_h * 0.25)   # 尖顶针
    if spec.get("door", True):   # 正面一扇门（略凸出塔身）
        dw, dh = r0 * 0.5, min(zb * 0.18, h * 0.12)
        _box(m, "door", -dw / 2, dw / 2, -r0 - 0.06, -r0 + 0.12, 0, dh)
    return m


# ───────────────────────── 房屋（可带风车叶片） ─────────────────────────

def house(spec: dict) -> Mesh:
    """方盒墙身 + 坡屋顶（gable 两坡 / hip 四坡），正面门、两侧窗；windmill 时在正面山墙装四片风车叶。
    wall_frac：墙高占总高（不含叶片）的比例；叶片半径 blade_r（米，LLM 按图给，缺省为宽的 0.75）。"""
    w, d, h = (float(x) for x in spec["size_m"])
    m = Mesh()
    blade_r = float(spec.get("blade_r", 0.45 * w)) if spec.get("windmill") else 0.0
    body_h = h - (blade_r * 0.6 if blade_r else 0)   # 叶片会高出屋脊：实测高度里扣掉一部分
    wall_h = body_h * max(0.35, min(0.8, float(spec.get("wall_frac", 0.58))))
    roof_h = body_h - wall_h
    blade_r = min(blade_r, 0.55 * w)   # 实测宽度里已经包含叶片：叶片直径不超过总宽
    hw, hd = w / 2 * (0.62 if blade_r else 0.86), d / 2 * (0.75 if blade_r else 0.86)
    ov = min(hw, hd) * 0.14
    if spec.get("base", True):   # 石砌勒脚
        _box(m, "stone", -hw - 0.05, hw + 0.05, -hd - 0.05, hd + 0.05, 0, wall_h * 0.12)
    _box(m, "plaster", -hw, hw, -hd, hd, wall_h * 0.12 if spec.get("base", True) else 0, wall_h)
    roof = spec.get("roof", "gable")
    z0, z1 = wall_h, wall_h + roof_h
    if roof == "hip":
        ridge = max(0.0, hw - hd) * 0.9
        a = [(-hw - ov, -hd - ov, z0), (hw + ov, -hd - ov, z0), (hw + ov, hd + ov, z0), (-hw - ov, hd + ov, z0)]
        r0, r1 = (-ridge, 0, z1), (ridge, 0, z1)
        m.add("roof", [a[0], a[1], r1, r0], (0, -1, 1))
        m.add("roof", [a[2], a[3], r0, r1], (0, 1, 1))
        m.add("roof", [a[1], a[2], r1], (1, 0, 1))
        m.add("roof", [a[3], a[0], r0], (-1, 0, 1))
        m.add("roof", a[::-1], (0, 0, -1))
    else:   # 两坡：屋脊沿 x，山墙朝前后
        a = [(-hw - ov, -hd - ov, z0), (hw + ov, -hd - ov, z0), (hw + ov, hd + ov, z0), (-hw - ov, hd + ov, z0)]
        r0, r1 = (-hw - ov, 0, z1), (hw + ov, 0, z1)
        if spec.get("ridge", "x") == "y":   # 屋脊沿 y：山墙朝左右
            r0, r1 = (0, -hd - ov, z1), (0, hd + ov, z1)
            m.add("roof", [a[1], a[2], r1, r0], (1, 0, 1))
            m.add("roof", [a[3], a[0], r0, r1], (-1, 0, 1))
            m.add("plaster", [(-hw, -hd, z0), (hw, -hd, z0), (0, -hd, z1)], (0, -1, 0))
            m.add("plaster", [(hw, hd, z0), (-hw, hd, z0), (0, hd, z1)], (0, 1, 0))
        else:
            m.add("roof", [a[0], a[1], r1, r0], (0, -1, 1))
            m.add("roof", [a[2], a[3], r0, r1], (0, 1, 1))
            m.add("plaster", [(-hw, -hd, z0), (-hw, hd, z0), (-hw, 0, z1)], (-1, 0, 0))
            m.add("plaster", [(hw, hd, z0), (hw, -hd, z0), (hw, 0, z1)], (1, 0, 0))
        m.add("roof", a[::-1], (0, 0, -1))
    # 门窗（略凸出墙面，避免 z 冲突）
    dw, dh = min(hw * 0.5, 0.9), min(wall_h * 0.62, 2.0)
    _box(m, "door", -dw / 2, dw / 2, -hd - 0.05, -hd + 0.02, 0, dh)
    win = max(0.35, min(hw, hd) * 0.32)
    for sx in (-1, 1):
        cx = sx * hw * 0.55
        if abs(cx) - win / 2 > dw / 2 + 0.1:   # 正面门两侧的窗（墙够宽时）
            _box(m, "window", cx - win / 2, cx + win / 2, -hd - 0.05, -hd + 0.02, wall_h * 0.45, wall_h * 0.45 + win)
    for sx in (-1, 1):   # 两侧山墙 / 侧墙各一扇窗
        x0 = hw if sx > 0 else -hw - 0.05
        _box(m, "window", x0 - (0.02 if sx > 0 else 0), x0 + (0.05 if sx > 0 else 0.07), -win / 2, win / 2,
             wall_h * 0.45, wall_h * 0.45 + win)
    if spec.get("windmill"):   # 风车：正面墙上方一个轮毂 + 四片格子叶（叶片在 y=-hd-0.4 的平面里）
        hz = min(z1 - roof_h * 0.25, wall_h + roof_h * 0.6)
        hy = -hd - ov - 0.35
        _box(m, "trim", -0.18, 0.18, hy - 0.1, -hd, hz - 0.18, hz + 0.18)
        bw = blade_r * 0.24
        rot = math.radians(float(spec.get("blade_angle", 15)))
        for k in range(4):
            a_ = rot + k * math.pi / 2
            ux, uz = math.cos(a_), math.sin(a_)
            px, pz = -uz, ux
            p0 = (0.2 * ux, 0.2 * uz)
            p1 = (blade_r * ux, blade_r * uz)
            quad = [(p0[0], hy, hz + p0[1]), (p1[0], hy, hz + p1[1]),
                    (p1[0] + px * bw, hy, hz + p1[1] + pz * bw), (p0[0] + px * bw * 0.5, hy, hz + p0[1] + pz * bw * 0.5)]
            m.add("sail", quad, (0, -1, 0))
            m.add("sail", quad[::-1], (0, 1, 0))
            # 叶杆
            m.add("trim", [(0, hy - 0.03, hz), (p1[0], hy - 0.03, hz + p1[1]),
                           (p1[0] + px * 0.08, hy - 0.03, hz + p1[1] + pz * 0.08), (px * 0.08, hy - 0.03, hz + pz * 0.08)],
                  (0, -1, 0))
    return m


# ───────────────────────── 码头 ─────────────────────────

def pier(spec: dict) -> Mesh:
    """木栈道：沿 +y（伸向远处的岸）铺横向木板，两侧每隔 post_gap 一根桩伸到水下；shape="L" 时远端向一侧拐出一段。
    size_m 的高 = 桥面离水高度 + 桩露出部分（桩向下伸 1 米入水，不计入）。"""
    w, d, h = (float(x) for x in spec["size_m"])
    m = Mesh()
    deck_z = max(0.3, h * 0.75)
    th = max(0.12, min(0.3, deck_z * 0.2))
    plank = max(0.25, min(0.5, w * 0.16))
    shape = spec.get("shape", "straight")
    main_w = w if shape != "L" else w * 0.45
    x0 = -w / 2 if shape == "L" else -main_w / 2
    segs = [(x0, x0 + main_w, -d / 2, d / 2)]
    if shape == "L":   # 远端拐出：沿 x 到另一侧
        segs.append((x0 + main_w, w / 2, d / 2 - main_w, d / 2))
    k = 0
    for sx0, sx1, sy0, sy1 in segs:
        along_y = (sy1 - sy0) >= (sx1 - sx0)
        L = (sy1 - sy0) if along_y else (sx1 - sx0)
        n = max(2, int(L / plank))
        for i in range(n):
            t0, t1 = i / n, (i + 1) / n - 0.08 / n
            if along_y:
                bx0, bx1, by0, by1 = sx0, sx1, sy0 + L * t0, sy0 + L * t1
            else:
                bx0, bx1, by0, by1 = sx0 + L * t0, sx0 + L * t1, sy0, sy1
            _box(m, "wood" if k % 2 == 0 else "wood_alt", bx0, bx1, by0, by1, deck_z - th, deck_z, skip_bottom=False)
            k += 1
        gap = float(spec.get("post_gap", 2.0))
        npost = max(2, int(L / gap) + 1)
        pr = max(0.1, min(0.22, w * 0.05))
        for i in range(npost):
            t = i / (npost - 1)
            for side in (0, 1):
                if along_y:
                    px = sx0 + pr if side == 0 else sx1 - pr
                    py = sy0 + pr + (L - 2 * pr) * t
                else:
                    px = sx0 + pr + (L - 2 * pr) * t
                    py = sy0 + pr if side == 0 else sy1 - pr
                _frustum(m, "post", 6, pr, pr, -1.0, deck_z - th, cx=px, cy=py)
                _cone(m, "post", 6, pr, deck_z - th, 0.001, cx=px, cy=py)
    return m


# ───────────────────────── 小船 ─────────────────────────

def boat(spec: dict) -> Mesh:
    """船体：沿 x 的放样（船头尖、船尾方），舷侧一条色带；cabin 时中后部一个小舱室。原点在吃水线中心。"""
    w, d, h = (float(x) for x in spec["size_m"])
    L, B = max(w, d), min(w, d)
    m = Mesh()
    hull_h = h * (0.55 if spec.get("cabin", True) else 0.9)
    draft = hull_h * 0.25
    sections = 7
    rings = []
    for i in range(sections + 1):
        t = i / sections               # 0 = 船尾，1 = 船头
        x = -L / 2 + L * t
        half = B / 2 * (1.0 if t < 0.55 else math.cos((t - 0.55) / 0.45 * math.pi / 2) ** 0.8)
        half = max(half, 0.02)
        rise = hull_h * 0.15 * max(0.0, t - 0.6) / 0.4   # 船头上翘
        rings.append([(x, -half, hull_h + rise), (x, -half * 0.7, -draft), (x, half * 0.7, -draft), (x, half, hull_h + rise)])
    stripe = float(spec.get("stripe", 0.35))   # 舷侧色带：船舷上沿往下 stripe×舷高
    for i in range(sections):
        a, b = rings[i], rings[i + 1]
        for side in (0, 2):
            p, q = (0, 1) if side == 0 else (2, 3)
            top_a, bot_a, top_b, bot_b = a[p if side == 0 else q], a[q if side == 0 else p], b[p if side == 0 else q], b[q if side == 0 else p]
            mid_a = tuple(top_a[c] + (bot_a[c] - top_a[c]) * stripe for c in range(3))
            mid_b = tuple(top_b[c] + (bot_b[c] - top_b[c]) * stripe for c in range(3))
            nrm = (0, -1 if side == 0 else 1, 0)
            m.add("hull_stripe", [top_a, top_b, mid_b, mid_a], nrm)
            m.add("hull", [mid_a, mid_b, bot_b, bot_a], nrm)
        m.add("hull", [a[1], b[1], b[2], a[2]], (0, 0, -1))
        m.add("deck", [a[0], a[3], b[3], b[0]], (0, 0, 1))
    m.add("hull", rings[0], (-1, 0, 0))
    if spec.get("cabin", True):
        cl, cw = L * 0.32, B * 0.6
        cx = -L * 0.08
        _box(m, "cabin", cx - cl / 2, cx + cl / 2, -cw / 2, cw / 2, hull_h, h * 0.92, top_role="cabin_roof")
        _box(m, "window", cx + cl / 2 - 0.02, cx + cl / 2 + 0.03, -cw * 0.35, cw * 0.35, hull_h + (h * 0.92 - hull_h) * 0.45,
             hull_h + (h * 0.92 - hull_h) * 0.8)
    return m


def _blob(mesh, role, cx, cy, z0, r, h, n=7, prof=((0.0, 0.45), (0.3, 1.0), (0.65, 0.85), (1.0, 0.0))):
    """绕竖轴的低多边形团块：prof = [(高度比例, 半径比例)]，最后一段收成尖 / 顶点。"""
    for (a, ra), (b, rb) in zip(prof, prof[1:]):
        za, zb = z0 + h * a, z0 + h * b
        if rb <= 1e-6:
            _cone(mesh, role, n, max(r * ra, 1e-3), za, zb - za, cx=cx, cy=cy)
        else:
            _frustum(mesh, role, n, max(r * ra, 1e-3), r * rb, za, zb, cap_bottom=(a == 0.0), cx=cx, cy=cy)


def tree(spec: dict) -> Mesh:
    """树：树干 + 树冠。shape：cypress（细高的柏树，纺锤形）/ round（圆冠）/ cluster（2–3 团矮灌木并排）。"""
    w, d, h = (float(x) for x in spec["size_m"])
    r = max(0.05, min(w, d) / 2)
    m = Mesh()
    shape = spec.get("shape", "round")
    tf = max(0.0, min(0.45, float(spec.get("trunk_frac", 0.15))))
    trunk_h = h * tf
    if trunk_h > 0.02:
        tr = max(0.02, r * 0.14)
        _frustum(m, "trunk", 6, tr, tr * 0.8, 0.0, trunk_h + h * 0.05, cap_top=True)
    z0 = max(0.0, trunk_h * 0.85)
    ch = h - z0
    if shape == "cypress":
        _blob(m, "canopy", 0.0, 0.0, z0, r, ch, n=7, prof=((0.0, 0.5), (0.22, 1.0), (0.55, 0.78), (0.82, 0.38), (1.0, 0.0)))
    elif shape == "cluster":
        k = max(2, min(3, int(spec.get("count", 3))))
        long_ = max(w, d)
        rr = min(r, long_ / (2 * k) * 1.25)
        for i in range(k):
            off = (i - (k - 1) / 2) * long_ / k
            cx, cy = (off, 0.0) if w >= d else (0.0, off)
            hh = ch * (1.0 if i == (k - 1) // 2 else 0.78)
            _blob(m, "canopy", cx, cy, z0, rr, hh, n=7, prof=((0.0, 0.6), (0.3, 1.0), (0.62, 0.85), (0.86, 0.45), (1.0, 0.0)))
    else:
        _blob(m, "canopy", 0.0, 0.0, z0, r, ch, n=8,
              prof=((0.0, 0.55), (0.2, 0.92), (0.45, 1.0), (0.7, 0.82), (0.88, 0.45), (1.0, 0.0)))
    return m



# ───────────────────────── 庄园（方塔 + 双坡石屋 + 拱门） ─────────────────────────

def estate(spec: dict) -> Mesh:
    """复合庄园：后方方形石塔（四坡陶瓦尖顶）+ 前方长石屋（双坡顶）+ 屋前石砌拱门 + 右侧侧屋。
    占满 size_m；原点在底面中心，-y 为正面。颜色角色：plaster/stone 墙、roof 瓦、trim 拱框。"""
    w, d, h = (float(x) for x in spec["size_m"])
    m = Mesh()
    # ── 方塔（偏后）──
    tw = min(w, d) * float(spec.get("tower_frac", 0.32))
    th = h * float(spec.get("tower_h_frac", 0.98))
    wall_h_t = th * 0.72
    roof_h_t = th - wall_h_t
    tx, ty = 0.0, d * float(spec.get("tower_back_frac", 0.28))
    hw_t = tw / 2
    if spec.get("base", True):
        _box(m, "stone", tx - hw_t - 0.06, tx + hw_t + 0.06, ty - hw_t - 0.06, ty + hw_t + 0.06, 0, wall_h_t * 0.1)
    _box(m, "plaster", tx - hw_t, tx + hw_t, ty - hw_t, ty + hw_t, wall_h_t * 0.1 if spec.get("base", True) else 0, wall_h_t)
    # 小窗
    win = max(0.25, tw * 0.22)
    for sx, sy in ((0, -1), (1, 0), (-1, 0)):
        if sy < 0:
            _box(m, "window", tx - win / 2, tx + win / 2, ty - hw_t - 0.05, ty - hw_t + 0.02,
                 wall_h_t * 0.55, wall_h_t * 0.55 + win)
        else:
            x0 = tx + sx * hw_t - (0.02 if sx > 0 else 0.05)
            x1 = tx + sx * hw_t + (0.05 if sx > 0 else 0.02)
            _box(m, "window", x0, x1, ty - win / 2, ty + win / 2, wall_h_t * 0.55, wall_h_t * 0.55 + win)
    # 四坡尖顶
    ov = tw * 0.08
    a = [(tx - hw_t - ov, ty - hw_t - ov, wall_h_t), (tx + hw_t + ov, ty - hw_t - ov, wall_h_t),
         (tx + hw_t + ov, ty + hw_t + ov, wall_h_t), (tx - hw_t - ov, ty + hw_t + ov, wall_h_t)]
    tip = (tx, ty, wall_h_t + roof_h_t)
    m.add("roof", [a[0], a[1], tip], (0, -1, 1))
    m.add("roof", [a[1], a[2], tip], (1, 0, 1))
    m.add("roof", [a[2], a[3], tip], (0, 1, 1))
    m.add("roof", [a[3], a[0], tip], (-1, 0, 1))
    m.add("roof", a[::-1], (0, 0, -1))

    # ── 主石屋（偏前，双坡）──
    hw = w * 0.42
    hd = d * float(spec.get("house_depth_frac", 0.28))  # 略加深主屋进深，减轻「纵深偏短」
    hx, hy = -w * 0.05, -d * 0.10
    house_h = h * float(spec.get("house_h_frac", 0.62))
    wall_h = house_h * float(spec.get("wall_frac", 0.58))
    roof_h = house_h - wall_h
    if spec.get("base", True):
        _box(m, "stone", hx - hw - 0.05, hx + hw + 0.05, hy - hd - 0.05, hy + hd + 0.05, 0, wall_h * 0.12)
    _box(m, "plaster", hx - hw, hx + hw, hy - hd, hy + hd, wall_h * 0.12 if spec.get("base", True) else 0, wall_h)
    ov = min(hw, hd) * 0.12
    z0, z1 = wall_h, wall_h + roof_h
    a = [(hx - hw - ov, hy - hd - ov, z0), (hx + hw + ov, hy - hd - ov, z0),
         (hx + hw + ov, hy + hd + ov, z0), (hx - hw - ov, hy + hd + ov, z0)]
    r0, r1 = (hx - hw - ov, hy, z1), (hx + hw + ov, hy, z1)
    m.add("roof", [a[0], a[1], r1, r0], (0, -1, 1))
    m.add("roof", [a[2], a[3], r0, r1], (0, 1, 1))
    m.add("plaster", [(hx - hw, hy - hd, z0), (hx - hw, hy + hd, z0), (hx - hw, hy, z1)], (-1, 0, 0))
    m.add("plaster", [(hx + hw, hy + hd, z0), (hx + hw, hy - hd, z0), (hx + hw, hy, z1)], (1, 0, 0))
    m.add("roof", a[::-1], (0, 0, -1))
    # 门窗
    dw, dh = min(hw * 0.35, 0.9), min(wall_h * 0.55, 2.0)
    _box(m, "door", hx - dw / 2, hx + dw / 2, hy - hd - 0.05, hy - hd + 0.02, 0, dh)
    win = max(0.3, min(hw, hd) * 0.28)
    for sx in (-1, 1):
        cx = hx + sx * hw * 0.55
        _box(m, "window", cx - win / 2, cx + win / 2, hy - hd - 0.05, hy - hd + 0.02,
             wall_h * 0.42, wall_h * 0.42 + win)

    # ── 右侧侧屋（稍方正）──
    if spec.get("wing", True):
        sw, sd = w * 0.22, d * 0.18
        sx, sy = w * 0.28, d * 0.02
        sh = h * 0.48
        swall = sh * 0.6
        _box(m, "plaster", sx - sw / 2, sx + sw / 2, sy - sd / 2, sy + sd / 2, 0, swall)
        ov = min(sw, sd) * 0.1
        a = [(sx - sw / 2 - ov, sy - sd / 2 - ov, swall), (sx + sw / 2 + ov, sy - sd / 2 - ov, swall),
             (sx + sw / 2 + ov, sy + sd / 2 + ov, swall), (sx - sw / 2 - ov, sy + sd / 2 + ov, swall)]
        tip = (sx, sy, sh)
        m.add("roof", [a[0], a[1], tip], (0, -1, 1))
        m.add("roof", [a[1], a[2], tip], (1, 0, 1))
        m.add("roof", [a[2], a[3], tip], (0, 1, 1))
        m.add("roof", [a[3], a[0], tip], (-1, 0, 1))

    # ── 前方石砌拱门 ──
    if spec.get("arch", True):
        aw = w * float(spec.get("arch_w_frac", 0.38))
        ad = max(0.35, d * 0.06)
        ah = h * float(spec.get("arch_h_frac", 0.42))
        ax, ay = hx, hy - hd - ad * 0.9 - 0.15
        # 两侧立柱 + 顶梁，中间留拱洞（用薄墙拼出圆拱轮廓）
        pier_w = aw * 0.18
        _box(m, "stone", ax - aw / 2, ax - aw / 2 + pier_w, ay - ad / 2, ay + ad / 2, 0, ah * 0.72)
        _box(m, "stone", ax + aw / 2 - pier_w, ax + aw / 2, ay - ad / 2, ay + ad / 2, 0, ah * 0.72)
        # 拱顶：若干段折线近似半圆
        n = 6
        import math as _math
        r = (aw / 2 - pier_w * 0.15)
        z_spring = ah * 0.55
        rise = ah * 0.35
        prev = None
        for i in range(n + 1):
            ang = _math.pi * i / n
            x = ax + r * _math.cos(_math.pi - ang)
            z = z_spring + rise * _math.sin(ang)
            # 外侧厚度
            outer = aw / 2 + 0.05
            ox = ax + outer * _math.cos(_math.pi - ang) if 0 < i < n else (ax - outer if i == 0 else ax + outer)
            # 用四边形段：内弧 → 外弧
            if prev is not None:
                (x0, z0, ox0), (x1, z1, ox1) = prev, (x, z, ox if 0 < i < n else (ax - outer if i == 0 else ax + outer))
                # 简化：内外两点用箱体柱段
                _box(m, "trim", min(x0, x1) - 0.08, max(x0, x1) + 0.08, ay - ad / 2, ay + ad / 2,
                     min(z0, z1) - 0.05, max(z0, z1) + 0.08)
            prev = (x, z, ox if 0 < i < n else (ax - outer if i == 0 else ax + outer))
        # 顶梁压顶
        _box(m, "stone", ax - aw / 2 - 0.05, ax + aw / 2 + 0.05, ay - ad / 2 - 0.02, ay + ad / 2 + 0.02,
             ah * 0.88, ah)
    return m


def flower(spec: dict) -> Mesh:
    """花 / 绿叶：按 shape 做可读花形，避免退化成圆冠树球。
    shape:
      tulip   — 细茎 + 杯状花头（开口朝上的锥台）
      spike   — 细茎 + 沿高叠放的小花穗（红蜀葵/穗花）
      blossom — 短茎 + 2–4 个小圆花头并排
      daisy   — 细茎 + 扁圆心 + 一圈瓣片
      foliage — 低矮宽叶丛（偏亮绿，无深色树干球）
    角色：petal/center 花头，stem 茎，leaf 叶。
    """
    w, d, h = (float(x) for x in spec["size_m"])
    r = max(0.02, min(w, d) / 2)
    m = Mesh()
    shape = spec.get("shape", "blossom")
    stem_frac = max(0.05, min(0.55, float(spec.get("stem_frac", 0.35))))

    if shape == "foliage":
        k = max(2, min(5, int(spec.get("count", 3))))
        long_ = max(w, d)
        rr = min(r, long_ / (2 * k) * 1.35)
        for i in range(k):
            off = (i - (k - 1) / 2) * long_ / max(k, 1)
            cx, cy = (off, 0.0) if w >= d else (0.0, off)
            hh = h * (1.0 if i == (k - 1) // 2 else 0.72)
            _blob(m, "leaf", cx, cy, 0.0, rr * 1.05, hh, n=7,
                  prof=((0.0, 0.55), (0.25, 1.0), (0.55, 0.9), (0.82, 0.5), (1.0, 0.0)))
        return m

    stem_h = h * stem_frac
    stem_r = max(0.008, r * float(spec.get("stem_r_frac", 0.08)))
    if stem_h > 0.015:
        _frustum(m, "stem", 6, stem_r, stem_r * 0.75, 0.0, stem_h + h * 0.02, cap_top=True)

    # 基叶（郁金香默认少叶，避免同机位取样被叶绿拖成棕）
    want_leaves = spec.get("leaves")
    if want_leaves is None:
        want_leaves = shape not in ("tulip", "spike")
    if want_leaves and shape != "spike":
        leaf_r = r * (0.35 if shape == "tulip" else 0.55)
        leaf_h = max(0.04, h * (0.12 if shape == "tulip" else 0.18))
        angs = (0.0, 3.14) if shape == "tulip" else (0.0, 2.1, 4.2)
        for ang in angs:
            import math as _m
            lx = leaf_r * 0.55 * _m.cos(ang)
            ly = leaf_r * 0.55 * _m.sin(ang)
            _blob(m, "leaf", lx, ly, 0.0, leaf_r * 0.45, leaf_h, n=5,
                  prof=((0.0, 0.4), (0.4, 1.0), (1.0, 0.0)))

    z0 = stem_h * (0.75 if shape == "tulip" else 0.9)
    head_h = max(0.04, h - z0)

    if shape == "tulip":
        # 杯状：下窄上宽开口（更大花头占比）
        br = r * 0.28
        tr = r * 1.05
        _frustum(m, "petal", 8, br, tr, z0, z0 + head_h * 0.85, cap_bottom=True)
        # 内侧略收的唇
        _frustum(m, "petal", 8, tr * 0.92, tr * 0.7, z0 + head_h * 0.7, z0 + head_h, cap_top=False)
        _frustum(m, "center", 6, br * 0.5, br * 0.35, z0, z0 + head_h * 0.45, cap_top=True)
    elif shape == "spike":
        n = max(4, min(10, int(spec.get("florets", 7))))
        for i in range(n):
            t = i / max(n - 1, 1)
            zz = z0 + head_h * t
            rr = r * (0.55 + 0.35 * (1.0 - abs(t - 0.55) * 1.4))
            hh = head_h / n * 1.35
            _blob(m, "petal", 0.0, 0.0, zz, rr, hh, n=6,
                  prof=((0.0, 0.5), (0.35, 1.0), (0.75, 0.7), (1.0, 0.0)))
        # 顶芽
        _cone(m, "petal", 6, r * 0.25, z0 + head_h * 0.92, head_h * 0.12)
    elif shape == "daisy":
        # 扁圆心 + 一圈瓣
        cr = r * 0.35
        _frustum(m, "center", 8, cr, cr * 0.9, z0 + head_h * 0.35, z0 + head_h * 0.7, cap_bottom=True, cap_top=True)
        import math as _m
        petals = max(6, min(12, int(spec.get("petals", 8))))
        for i in range(petals):
            ang = 2 * _m.pi * i / petals
            px = r * 0.55 * _m.cos(ang)
            py = r * 0.55 * _m.sin(ang)
            _blob(m, "petal", px, py, z0 + head_h * 0.25, r * 0.28, head_h * 0.55, n=5,
                  prof=((0.0, 0.4), (0.4, 1.0), (1.0, 0.15)))
    else:  # blossom
        k = max(2, min(4, int(spec.get("count", 3))))
        import math as _m
        for i in range(k):
            ang = 2 * _m.pi * i / k
            rad = r * (0.35 if k > 1 else 0.0)
            cx, cy = rad * _m.cos(ang), rad * _m.sin(ang)
            hh = head_h * (0.85 if i else 1.0)
            _blob(m, "petal", cx, cy, z0, r * (0.55 if k > 1 else 0.85), hh, n=7,
                  prof=((0.0, 0.5), (0.3, 1.0), (0.65, 0.8), (1.0, 0.0)))
            _blob(m, "center", cx, cy, z0 + hh * 0.35, r * 0.18, hh * 0.35, n=5,
                  prof=((0.0, 0.6), (0.5, 1.0), (1.0, 0.0)))
    return m



BUILDERS = {"tower": tower, "house": house, "pier": pier, "boat": boat, "tree": tree, "estate": estate, "flower": flower}
DEFAULT_COLORS = {"body": "#EEEEEE", "trim": "#3A3A40", "window": "#2E3640", "roof": "#B5543F", "door": "#6B4A34",
                  "plaster": "#C9A27A", "stone": "#9C8F84", "sail": "#E8DCC8", "wood": "#B98552", "wood_alt": "#A87648",
                  "post": "#7A5634", "hull": "#F2F2F0", "hull_stripe": "#3E6FA8", "deck": "#C8A47A", "cabin": "#F2F2F0",
                  "cabin_roof": "#3E6FA8", "canopy": "#6E9A45", "trunk": "#6B5238", "petal": "#C44B3A", "stem": "#5A6B3E", "leaf": "#4F8A55", "center": "#E8C84A"}


def validate(spec: dict) -> str | None:
    if not isinstance(spec, dict) or spec.get("type") not in TYPES:
        return f"type 必须是 {TYPES} 之一"
    s = spec.get("size_m")
    if not (isinstance(s, (list, tuple)) and len(s) == 3 and all(isinstance(x, (int, float)) and x > 0 for x in s)):
        return "size_m 需要三个正数 [宽, 深, 高]"
    return None


def build(spec: dict) -> dict:
    """→ {"parts": 角色 → {"v", "f"}, "palette": 角色 → 色值}。色带角色 band0.. 的颜色取自 spec["bands"]。"""
    err = validate(spec)
    if err:
        raise ValueError(err)
    mesh = BUILDERS[spec["type"]](spec)
    parts = mesh.as_dict()
    pal = {**DEFAULT_COLORS, **{k: v for k, v in (spec.get("colors") or {}).items() if isinstance(v, str)}}
    for k, b in enumerate(spec.get("bands") or []):
        pal[f"band{k}"] = b["hex"]
    pal["outline"] = (spec.get("colors") or {}).get("outline", "#434244")
    return {"parts": parts, "palette": {r: pal.get(r, "#888888") for r in list(parts) + ["outline"]}}
