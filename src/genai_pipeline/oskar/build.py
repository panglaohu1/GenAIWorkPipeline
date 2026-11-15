"""角点状态 → 网格数据（按材质角色分桶）。纯 Python，确定性。

几何 = 对偶格上的 marching squares：四边形 q 的第 k 个四分之一格属于角点 vs[k]；
实心格与空格之间的中线就是墙面。墙面、顶面、底面的细节由模块（局部坐标 s,t,d）生成，
模块变体由 WFC 决定（x-timeline §2.2：门窗等表面装饰另跑一个小 WFC）。
"""
from __future__ import annotations

import math
import random
from collections import defaultdict

from .program import BUILDING_KINDS, State
from .wfc import WFC

WALL_ROLE = {"rock": "rock", "plinth": "stone", "wall": "stone", "main": "plaster", "wing": "plaster",
             "tower": "plaster", "house": "plaster"}


class Mesh:
    def __init__(self):
        self.parts: dict[str, dict] = defaultdict(lambda: {"v": [], "f": []})

    def add(self, role: str, pts, want=None):
        pts = [tuple(p) for p in pts]
        if want is not None:
            nx = ny = nz = 0.0
            for i in range(len(pts)):
                x0, y0, z0 = pts[i]
                x1, y1, z1 = pts[(i + 1) % len(pts)]
                nx += (y0 - y1) * (z0 + z1)
                ny += (z0 - z1) * (x0 + x1)
                nz += (x0 - x1) * (y0 + y1)
            if nx * want[0] + ny * want[1] + nz * want[2] < 0:
                pts = pts[::-1]
        part = self.parts[role]
        base = len(part["v"])
        part["v"] += [[round(c, 4) for c in p] for p in pts]
        part["f"].append(list(range(base, base + len(pts))))

    def as_dict(self) -> dict:
        return {k: v for k, v in self.parts.items() if v["f"]}


class Noise:
    """平滑向量噪声：岩体整体形变（位置的函数，共享顶点天然一致，不会裂缝）。
    两个倍频：低频定大轮廓（波长 11–35 m），中频打碎直边（波长 4–6 m）。"""

    def __init__(self, seed: int, freq=(0.18, 0.55), octaves=4):
        r = random.Random(seed * 131 + 7 + int(freq[0] * 1000))
        self.w = [[(r.uniform(-1, 1), r.uniform(-1, 1), r.uniform(-0.6, 0.6), r.uniform(0, 6.28), r.uniform(*freq))
                   for _ in range(octaves)] for _ in range(3)]

    def __call__(self, x, y, z):
        out = []
        for comp in self.w:
            s = 0.0
            for ax, ay, az, ph, f in comp:
                s += math.sin((ax * x + ay * y + az * z) * f + ph)
            out.append(s / len(comp))
        return out


# ───────────────────────── 墙面模块（局部坐标 s 沿墙, t 向上, d 向外） ─────────────────────────
# 立面以「面板」为单位：一条对偶边（实心顶点 v → 空顶点 w）在第 h 层，由相邻两个四边形里的两段墙组成，
# 两段在边中点相接。开口（窗 / 门）以边中点为中心、按米计宽，跨两段墙（面板 s=0 端即边中点）。

def _arc(a, tb, rise, n=5):
    return [(a * i / n, tb + rise * math.sqrt(max(0.0, 1 - (i / n) ** 2))) for i in range(n + 1)]


def _open_piece(face, a, t0, t1, depth, role, pane, arched=False, rise=0.0):
    """面板半边：s∈[0,a] 为开口（中点端无侧面），其余为墙。arched 时 t1 为拱脚高，拱顶 t1+rise。"""
    face([(a, 0, 0), (1, 0, 0), (1, 1, 0), (a, 1, 0)], role, "n")
    if t0 > 0:
        face([(0, 0, 0), (a, 0, 0), (a, t0, 0), (0, t0, 0)], role, "n")
        face([(0, t0, 0), (a, t0, 0), (a, t0, -depth), (0, t0, -depth)], role, "+t")
    if arched:
        arc = _arc(a, t1, rise)  # 从中点(拱顶)到 s=a(拱脚)
        for (sa, ta), (sb, tb) in zip(arc, arc[1:]):
            face([(sa, ta, 0), (sb, tb, 0), (sb, 1, 0), (sa, 1, 0)], role, "n")
            face([(sa, ta, 0), (sb, tb, 0), (sb, tb, -depth), (sa, ta, -depth)], role, "-t")
            face([(sa, t0, -depth), (sb, t0, -depth), (sb, tb, -depth), (sa, ta, -depth)], pane, "n")
        top_at_a = t1
    else:
        face([(0, t1, 0), (a, t1, 0), (a, 1, 0), (0, 1, 0)], role, "n")
        face([(0, t1, 0), (a, t1, 0), (a, t1, -depth), (0, t1, -depth)], role, "-t")
        face([(0, t0, -depth), (a, t0, -depth), (a, t1, -depth), (0, t1, -depth)], pane, "n")
        top_at_a = t1
    face([(a, t0, 0), (a, top_at_a, 0), (a, top_at_a, -depth), (a, t0, -depth)], role, "-s")


def wall_piece(tile: str, face, role: str, parity: int, ln: float, level_h: float):
    alt = {"rock": "rock_alt", "stone": "stone_alt", "plaster": "plaster"}.get(role, role)
    if tile in ("blank", "wall_plain", "stone_plain"):
        face([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], role, "n")
    elif tile == "stone_band":
        face([(0, 0, 0), (1, 0, 0), (1, 0.82, 0), (0, 0.82, 0)], role, "n")
        face([(0, 0.82, 0), (1, 0.82, 0), (1, 1, 0), (0, 1, 0)], alt, "n")
    elif tile == "window":
        _open_piece(face, min(0.8, 0.36 / ln), 0.34, 0.74, 0.14, role, "window")
    elif tile == "arch_win":
        _open_piece(face, min(0.8, 0.4 / ln), 0.2, 0.56, 0.16, role, "window", arched=True, rise=0.22)
    elif tile == "door":
        _open_piece(face, min(0.8, 0.46 / ln), 0.0, 0.46, 0.2, role, "door", arched=True, rise=0.2)
    elif tile == "loggia":
        # 拱廊：高拱开口 + 深进深，同层连续成排（地中海宫城顶层）
        _open_piece(face, min(0.85, 0.62 / ln), 0.06, 0.58, 0.45, role, "window", arched=True, rise=0.26)
    elif tile == "massive":
        face([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], role if parity % 3 else alt, "n")
    elif tile == "slit":
        _open_piece(face, min(0.5, 0.09 / ln), 0.3, 0.8, 0.3, role, "window")
    elif tile == "strata":
        # 层状石灰岩：每层底部一条内收暗缝（像素级细线的几何版），上下两段明度交替
        a, b = (role, alt) if parity % 2 == 0 else (alt, role)
        face([(0, 0.1, 0), (1, 0.1, 0), (1, 0.55, 0), (0, 0.55, 0)], a, "n")
        face([(0, 0.55, 0), (1, 0.55, 0), (1, 1, 0), (0, 1, 0)], b, "n")
        face([(0, 0, -0.14), (1, 0, -0.14), (1, 0.1, -0.14), (0, 0.1, -0.14)], "rock_dark", "n")
        face([(0, 0.1, 0), (1, 0.1, 0), (1, 0.1, -0.14), (0, 0.1, -0.14)], a, "-t")
        face([(0, 0, 0), (1, 0, 0), (1, 0, -0.14), (0, 0, -0.14)], "rock_dark", "+t")
    elif tile == "crack":
        face([(0, 0, 0), (0.44, 0, 0), (0.44, 1, 0), (0, 1, 0)], role, "n")
        face([(0.56, 0, 0), (1, 0, 0), (1, 1, 0), (0.56, 1, 0)], role, "n")
        face([(0.44, 0, 0), (0.44, 1, 0), (0.47, 1, -0.35), (0.47, 0, -0.35)], "rock_dark", "+s")
        face([(0.56, 0, 0), (0.56, 1, 0), (0.53, 1, -0.35), (0.53, 0, -0.35)], "rock_dark", "-s")
        face([(0.47, 0, -0.35), (0.53, 0, -0.35), (0.53, 1, -0.35), (0.47, 1, -0.35)], "rock_dark", "n")
    elif tile == "ledge":
        face([(0, 0, 0), (1, 0, 0), (1, 0.74, 0), (0, 0.74, 0)], role, "n")
        face([(0, 0.74, 0.42), (1, 0.74, 0.42), (1, 1, 0.42), (0, 1, 0.42)], alt, "n")
        face([(0, 0.74, 0), (1, 0.74, 0), (1, 0.74, 0.42), (0, 0.74, 0.42)], "rock_dark", "-t")
        face([(0, 1, 0), (1, 1, 0), (1, 1, 0.42), (0, 1, 0.42)], "grass", "+t")
        face([(0, 0.74, 0), (0, 1, 0), (0, 1, 0.42), (0, 0.74, 0.42)], alt, "-s")
        face([(1, 0.74, 0), (1, 1, 0), (1, 1, 0.42), (1, 0.74, 0.42)], alt, "+s")
    elif tile == "notch":
        face([(0, 0.6, 0), (1, 0.6, 0), (1, 1, 0), (0, 1, 0)], role, "n")
        face([(0, 0, -0.75), (1, 0, -0.75), (1, 0.6, -0.75), (0, 0.6, -0.75)], "rock_dark", "n")
        face([(0, 0.6, 0), (1, 0.6, 0), (1, 0.6, -0.75), (0, 0.6, -0.75)], "rock_dark", "-t")
        face([(0, 0, 0), (1, 0, 0), (1, 0, -0.75), (0, 0, -0.75)], "rock_dark", "+t")
        face([(0, 0, 0), (0, 0.6, 0), (0, 0.6, -0.75), (0, 0, -0.75)], "rock_dark", "-s")
        face([(1, 0, 0), (1, 0.6, 0), (1, 0.6, -0.75), (1, 0, -0.75)], "rock_dark", "+s")
    else:
        face([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], role, "n")


# ───────────────────────── 瓦片集与相容规则 ─────────────────────────
OPEN = {"window", "arch_win", "door"}
B_TILES = {"blank", "window", "arch_win", "door"}
ALL_TILES = B_TILES | {"wall_plain", "slit", "stone_plain", "stone_band", "strata", "crack", "ledge", "notch",
                       "massive", "loggia"}
WEIGHTS = {"blank": 0.55, "window": 0.5, "arch_win": 0.32, "door": 0.12, "wall_plain": 0.82, "slit": 0.18,
           "stone_plain": 0.7, "stone_band": 0.3, "strata": 0.75, "crack": 0.16, "ledge": 0.16, "notch": 1.4,
           "massive": 0.6, "loggia": 1.0}
# 竖向棱柱岩（十二门徒）：整块岩面为主，层缝减少，裂隙通高
COLUMNAR_WEIGHTS = {**WEIGHTS, "strata": 0.3, "massive": 0.75, "crack": 0.34, "ledge": 0.05}


def _allowed() -> dict[str, set[tuple[str, str]]]:
    v, h, c = set(), set(), set()
    for a in ALL_TILES:
        for b in ALL_TILES:
            # 竖向（a 在下，b 在上）：窗列上下对齐（开 / 实同列），门只在底部；岩檐不叠
            if a in B_TILES and b in B_TILES:
                if b != "door" and (a in OPEN) == (b in OPEN):
                    v.add((a, b))
            elif not (a == b == "ledge") and not (a == "crack" and b != "crack"):
                v.add((a, b))   # 裂隙向上贯通到岩柱顶（竖向节理）
            # 横向（沿立面直线延续）：门不相邻、裂隙不相邻
            if not (a == b == "door" or a == b == "crack"):
                h.add((a, b))
            # 凸角（同一格的两个立面在墙角相接）：墙角不同时开口，保持结实
            if not (a in OPEN and b in OPEN):
                c.add((a, b))
    return {"v": v, "h": h, "c": c}


ALLOWED = _allowed()


# ───────────────────────── 主构建 ─────────────────────────

class TerrainField:
    """平滑后的岩体顶面高度场 H(x, y)（米），由所有岩体柱的顶高按高斯核加权得到，预先栅格化。

    用途（x.com/OskSta 2024-03-18 “蘑菇化”与 relax pass；2022-10-10 cage deformation）：
    低于局部平滑顶面的岩体顶点沿 −∇H（下坡、向外）外移，外移量随“低于顶面的深度”增长——
    高处收、低处放，崖面因此成坡、塔状山体不再上宽下窄；只依赖位置，共享顶点一致、不会裂缝。"""

    def __init__(self, st: State, sigma: float = 3.2, step: float = 1.0):
        cols = defaultdict(int)
        for (v, h), c in st.cells.items():
            if c.kind == "rock":
                cols[v] = max(cols[v], h + 1)
        self.ok = bool(cols)
        if not self.ok:
            return
        pts = [(st.grid.verts[v][0], st.grid.verts[v][1], st.z(t)) for v, t in cols.items()]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        pad = sigma * 2.5
        self.x0, self.y0, self.step = min(xs) - pad, min(ys) - pad, step
        self.nx = int((max(xs) + pad - self.x0) / step) + 2
        self.ny = int((max(ys) + pad - self.y0) / step) + 2
        inv = 1.0 / (2 * sigma * sigma)
        floor = st.base_z
        self.h = []
        for j in range(self.ny):
            y = self.y0 + j * step
            row = []
            near = [p for p in pts if abs(p[1] - y) < pad]
            for i in range(self.nx):
                x = self.x0 + i * step
                sw = sh = 0.0
                for px, py, pz in near:
                    dx = px - x
                    if abs(dx) < pad:
                        w = math.exp(-(dx * dx + (py - y) ** 2) * inv)
                        sw += w
                        sh += w * pz
                # 远离岩体时回落到底面：权重和小于 1 时与底面混合（外缘自然向下）
                row.append(sh / sw * min(1.0, sw) + floor * (1 - min(1.0, sw)) if sw > 1e-6 else floor)
            self.h.append(row)

    def _at(self, i, j):
        i = min(max(i, 0), self.nx - 1)
        j = min(max(j, 0), self.ny - 1)
        return self.h[j][i]

    def sample(self, x, y):
        fx, fy = (x - self.x0) / self.step, (y - self.y0) / self.step
        i, j = int(math.floor(fx)), int(math.floor(fy))
        u, v = fx - i, fy - j
        a, b = self._at(i, j), self._at(i + 1, j)
        c, d = self._at(i, j + 1), self._at(i + 1, j + 1)
        return (a * (1 - u) + b * u) * (1 - v) + (c * (1 - u) + d * u) * v

    def grad(self, x, y):
        e = self.step
        return ((self.sample(x + e, y) - self.sample(x - e, y)) / (2 * e),
                (self.sample(x, y + e) - self.sample(x, y - e)) / (2 * e))


class OutlineField:
    """每一层“实心格占据”的平滑覆盖场 M_h(x, y)，用于把岩体平面轮廓收向平滑等值线（软化格网的锯齿）。

    对应 x.com/OskSta 2023-06-19 层级平滑：硬边（崖顶 / 台缘的上下折线）保持不动——这里只在水平面内移动、z 不变；
    格网锯齿（瓦片边界）被抹平。层与层之间按高度平滑过渡，只依赖位置，共享顶点一致。"""

    def __init__(self, st: State, sigma: float = 1.7, step: float = 0.5):
        self.st, self.step = st, step
        by_level = defaultdict(list)
        built = defaultdict(list)    # 非岩体（台基、城墙、建筑）：其附近的岩体不变形，避免与建筑脱开
        for (v, h), c in st.cells.items():
            by_level[h].append(st.grid.verts[v])
            if c.kind != "rock":
                built[h].append(st.grid.verts[v])
        self.ok = bool(by_level)
        if not self.ok:
            return
        allp = [p for ps in by_level.values() for p in ps]
        pad = sigma * 3
        self.x0, self.y0 = min(p[0] for p in allp) - pad, min(p[1] for p in allp) - pad
        self.nx = int((max(p[0] for p in allp) + pad - self.x0) / step) + 2
        self.ny = int((max(p[1] for p in allp) + pad - self.y0) / step) + 2
        inv = 1 / (2 * sigma * sigma)
        # 覆盖场归一：用格网顶点的实际面密度（不规则格网各处密度相近），内部约为 1，轮廓取 0.5
        g = st.grid
        span = max(math.hypot(*v) for v in g.verts) or 1.0
        density = len(g.verts) / (math.pi * span * span)
        self.norm = 2 * math.pi * sigma * sigma * density
        self.levels, self.built = {}, {}
        for store, src in ((self.levels, by_level), (self.built, built)):
            self._fill(store, src, inv)

    def _fill(self, store, by_level, inv):
        step = self.step
        for h, pts in by_level.items():
            buckets = defaultdict(list)
            for p in pts:
                buckets[(int((p[0] - self.x0) // 3), int((p[1] - self.y0) // 3))].append(p)
            grid = []
            for j in range(self.ny):
                y = self.y0 + j * step
                row = []
                for i in range(self.nx):
                    x = self.x0 + i * step
                    bi, bj = int((x - self.x0) // 3), int((y - self.y0) // 3)
                    sv = 0.0
                    for di in (-1, 0, 1):
                        for dj in (-1, 0, 1):
                            for px, py in buckets.get((bi + di, bj + dj), ()):
                                sv += math.exp(-((px - x) ** 2 + (py - y) ** 2) * inv)
                    row.append(min(1.0, sv / self.norm))
                grid.append(row)
            store[h] = grid

    def protect(self, x, y, z):
        """1 = 可以自由变形；靠近非岩体格、瀑布水幕时降到 0（水幕与建筑要贴着原岩面）。"""
        f = (z - self.st.base_z) / self.st.level_h
        h = int(math.floor(f))
        b = max(self._lvl(h, x, y, self.built), self._lvl(h - 1, x, y, self.built), self._lvl(h + 1, x, y, self.built))
        keep = max(0.0, 1.0 - 2.5 * b)
        for wf in self.st.waterfalls:
            (ax, ay), (dx, dy), w = wf["at"], wf["dir"], wf["w"]
            t = max(0.0, min(60.0, (x - ax) * dx + (y - ay) * dy))
            d = math.hypot(x - ax - dx * t, y - ay - dy * t)
            keep = min(keep, max(0.0, min(1.0, (d - w / 2 - 1.0) / 1.5)))
        return keep

    def _lvl(self, h, x, y, levels=None):
        grid = (self.levels if levels is None else levels).get(h)
        if grid is None:
            return 0.0
        fx, fy = (x - self.x0) / self.step, (y - self.y0) / self.step
        i, j = int(math.floor(fx)), int(math.floor(fy))
        if not (0 <= i < self.nx - 1 and 0 <= j < self.ny - 1):
            return 0.0
        u, v = fx - i, fy - j
        return (grid[j][i] * (1 - u) + grid[j][i + 1] * u) * (1 - v) + (grid[j + 1][i] * (1 - u) + grid[j + 1][i + 1] * u) * v

    def value(self, x, y, z):
        f = (z - self.st.base_z) / self.st.level_h - 0.5     # 以层中心为节点
        h = int(math.floor(f))
        t = f - h
        t = 0.0 if t < 0.3 else 1.0 if t > 0.7 else (t - 0.3) / 0.4    # 层内大部分高度用本层轮廓，只在层交界处过渡
        return self._lvl(h, x, y) * (1 - t) + self._lvl(h + 1, x, y) * t

    def _raw_pull(self, h, x, y, cap):
        m = self._lvl(h, x, y)
        if not 0.33 < m < 0.96:   # 太小的孤立台面（整体覆盖不到一半）保持原样，不往中心塌
            return 0.0, 0.0
        e = self.step
        gx = (self._lvl(h, x + e, y) - self._lvl(h, x - e, y)) / (2 * e)
        gy = (self._lvl(h, x, y + e) - self._lvl(h, x, y - e)) / (2 * e)
        g2 = gx * gx + gy * gy
        if g2 < 1e-4:
            return 0.0, 0.0
        k = (0.5 - m) / g2
        dx, dy = gx * k, gy * k
        d = math.hypot(dx, dy)
        if d > cap:
            dx, dy = dx * cap / d, dy * cap / d
        return dx, dy

    def _disp(self, h, cap):
        """第 h 层的位移场，栅格化后做两遍盒式模糊：位移本身平滑，相邻顶点不会交叉折叠。"""
        key = (h, cap)
        cache = self.__dict__.setdefault("_disp_cache", {})
        if key in cache:
            return cache[key]
        if h not in self.levels:
            cache[key] = None
            return None
        nx, ny, step = self.nx, self.ny, self.step
        DX = [[0.0] * nx for _ in range(ny)]
        DY = [[0.0] * nx for _ in range(ny)]
        for j in range(ny):
            for i in range(nx):
                DX[j][i], DY[j][i] = self._raw_pull(h, self.x0 + i * step, self.y0 + j * step, cap)

        def blur(G, r=1):
            for _ in range(3):
                H = [[sum(row[max(0, i - r):i + r + 1]) / (min(nx, i + r + 1) - max(0, i - r)) for i in range(nx)] for row in G]
                G = [[sum(H[jj][i] for jj in range(max(0, j - r), min(ny, j + r + 1))) / (min(ny, j + r + 1) - max(0, j - r))
                      for i in range(nx)] for j in range(ny)]
            return G
        BX, BY = blur(DX), blur(DY)
        # 模糊会把窄带里的位移摊薄：乘回增益后再限幅，保留软化力度又不折叠
        for j in range(ny):
            for i in range(nx):
                bx, by = BX[j][i] * 1.3, BY[j][i] * 1.3
                d = math.hypot(bx, by)
                if d > cap:
                    bx, by = bx * cap / d, by * cap / d
                BX[j][i], BY[j][i] = bx, by
        cache[key] = (BX, BY)
        return cache[key]

    def _disp_at(self, h, x, y, cap):
        d = self._disp(h, cap)
        if d is None:
            return 0.0, 0.0
        fx, fy = (x - self.x0) / self.step, (y - self.y0) / self.step
        i, j = int(math.floor(fx)), int(math.floor(fy))
        if not (0 <= i < self.nx - 1 and 0 <= j < self.ny - 1):
            return 0.0, 0.0
        u, v = fx - i, fy - j
        out = []
        for G in d:
            out.append((G[j][i] * (1 - u) + G[j][i + 1] * u) * (1 - v) + (G[j + 1][i] * (1 - u) + G[j + 1][i + 1] * u) * v)
        return out[0], out[1]

    def pull(self, x, y, z, cap):
        """平滑位移场在 (x, y, z) 处的值（层间按高度过渡，规则同 value）。"""
        f = (z - self.st.base_z) / self.st.level_h - 0.5
        h = int(math.floor(f))
        t = f - h
        t = 0.0 if t < 0.3 else 1.0 if t > 0.7 else (t - 0.3) / 0.4
        cap = round(cap, 3)
        if cap <= 0:
            return 0.0, 0.0
        a = self._disp_at(h, x, y, cap) if t < 1 else (0.0, 0.0)
        b = self._disp_at(h + 1, x, y, cap) if t > 0 else (0.0, 0.0)
        return a[0] * (1 - t) + b[0] * t, a[1] * (1 - t) + b[1] * t


def make_deform(st: State, seed: int):
    """岩体形变：低频 + 中频噪声，加「逐层外鼓」——每层中段沿离开岩柱轴线的方向鼓出、层交界处收回，
    与层底暗缝配合形成层状石灰岩的锯齿剖面；只依赖位置与岩柱中心，连续无裂缝。"""
    low, mid = Noise(seed, (0.18, 0.55)), Noise(seed + 1, (1.0, 1.6), 3)
    facet = None
    if st.facet and st.facet[0] > 0:   # 块面起伏：波长约两块崖面宽，相邻顶点推出推进不同，面朝向各异
        fq = 2 * math.pi / (0.8 * st.facet[1])
        facet = (Noise(seed + 5, (fq * 0.75, fq * 1.25), 3), st.facet[0])
    centers = {}
    for (v, h), c in st.cells.items():
        if c.kind == "rock":
            x, y = st.grid.verts[v]
            a = centers.setdefault(c.group, [0.0, 0.0, 0])
            a[0] += x
            a[1] += y
            a[2] += 1
    centers = {gid: (a[0] / a[2], a[1] / a[2]) for gid, a in centers.items()}
    lh = st.level_h
    detail = st.rock_detail   # 岩面细节强度：噪声、逐层外鼓、竖向棱脊都按它缩放（目标图崖面是大块平面时接近 0）
    columnar = st.rock_style == "columnar"
    batter = st.batter if st.batter is not None else (0.32 if columnar else 0.5)   # 每低于平滑顶面 1 层外放的米数
    field = TerrainField(st) if batter > 0 else None
    field = field if field is not None and field.ok else None
    soften = st.soften if st.soften is not None else 0.0   # 平面轮廓软化的最大位移（米；program 决定默认值）
    outline = OutlineField(st) if (soften > 0 or batter > 0) else None
    outline = outline if outline is not None and outline.ok else None
    # 竖向棱脊：沿岩柱周向的起伏（与高度无关 → 贯通上下的棱与槽），波长约 3.2 米
    radius = defaultdict(float)
    for (v, h), c in st.cells.items():
        if c.kind == "rock" and c.group in centers:
            x, y = st.grid.verts[v]
            radius[c.group] = max(radius[c.group], math.hypot(x - centers[c.group][0], y - centers[c.group][1]))
    rr = random.Random(seed * 53 + 11)
    flutes = {gid: (max(5, round(2 * math.pi * max(radius[gid], 1.0) / 3.2)), rr.uniform(0, 6.28), rr.uniform(0, 6.28))
              for gid in sorted(centers)}

    def deform(p, kind, gid=None):
        if kind != "rock":
            return p
        keep = outline.protect(p[0], p[1], p[2]) if outline is not None else 1.0
        if outline is not None and soften > 0 and keep > 0:   # 先软化平面轮廓（格网锯齿），再叠加噪声与收分
            ox, oy = outline.pull(p[0], p[1], p[2], soften)
            p = (p[0] + ox * keep, p[1] + oy * keep, p[2])
        n1 = low(p[0], p[1], p[2])
        n2 = mid(p[0], p[1], p[2])
        # 低频起伏造出大块斜面（低多边形崖面的块面感），始终保留；中频碎纹按岩面细节缩放
        x = p[0] + n1[0] * 0.85 + n2[0] * 0.42 * detail
        y = p[1] + n1[1] * 0.85 + n2[1] * 0.42 * detail
        z = p[2] + n1[2] * 0.22 + n2[2] * 0.1 * detail
        if facet is not None:
            n3 = facet[0](p[0], p[1], p[2])
            x += n3[0] * facet[1]
            y += n3[1] * facet[1]
            z += n3[2] * facet[1] * 0.4
        if field is not None:   # 收分：高处收、低处放（基于平滑顶面，连续无裂缝）
            depth = (field.sample(p[0], p[1]) - p[2]) / lh
            if depth > 0.05:
                gx, gy = field.grad(p[0], p[1])
                gl = math.hypot(gx, gy)
                if gl > 0.02:
                    m = batter * min(depth, 5.0) * min(1.0, gl / 0.35) * keep
                    x -= gx / gl * m
                    y -= gy / gl * m
        c = centers.get(gid)
        if c is not None:
            frac = ((p[2] - st.base_z) / lh) % 1.0
            bulge = 0.5 * (1 - math.cos(2 * math.pi * frac)) * (0.1 if columnar else 0.38) * detail
            dx, dy = p[0] - c[0], p[1] - c[1]
            dl = math.hypot(dx, dy) or 1.0
            if columnar and gid in flutes:
                k, ph, ph2 = flutes[gid]
                s = math.sin(k * math.atan2(dy, dx) + ph)
                s = math.copysign(abs(s) ** 0.7, s)
                bulge += s * 0.5 * (0.75 + 0.25 * math.sin(p[2] * 0.23 + ph2)) * min(1.0, dl / 2.5) * detail
            x += dx / dl * bulge
            y += dy / dl * bulge
        return (x, y, z)

    return deform


def build_mesh(st: State, seed: int = 0) -> dict:
    g = st.grid
    mesh = Mesh()
    deform = make_deform(st, seed)
    occ = st.cells

    def cell(v, h):
        return occ.get((v, h))

    # 1. 墙段 → 面板（对偶边 v→w，第 h 层）
    segs = {}      # (q,k,side,h) -> info
    panels = {}    # (v,w,h) -> {"kind", "segs": [...]}
    for (v, h), c in occ.items():
        for q in g.vert_quads[v]:
            vs = g.quads[q]
            k = vs.index(v)
            for side, nb in (("n", vs[(k + 1) % len(vs)]), ("p", vs[(k - 1) % len(vs)])):
                if cell(nb, h) is None:
                    key = (q, k, side, h)
                    segs[key] = {"kind": c.kind, "v": v, "h": h, "w": nb}
                    panels.setdefault((v, nb, h), {"kind": c.kind, "segs": []})["segs"].append(key)
    # 2. 面板 WFC
    sea = st.sea_level
    med = st.style == "mediterranean"
    domains, rels = {}, []
    for pk, pn in panels.items():
        v, w, h = pk
        kind = pn["kind"]
        below = cell(v, h - 1)
        ground_out = h == 0 or cell(w, h - 1) is not None   # 门外有地面才开门（补救：Bonsai 常让门开在半空）
        if kind in BUILDING_KINDS:
            base = below is None or below.kind not in BUILDING_KINDS
            if med and kind != "house":
                # 地中海宫城：拱窗；主楼 / 翼楼顶层为连续拱廊
                top_floor = cell(v, h + 1) is None
                dom = {"loggia"} if kind in ("main", "wing") and top_floor and not base else {"blank", "arch_win"}
                if base and kind != "tower" and ground_out:
                    dom = {"blank", "arch_win", "door"}
            else:
                dom = {"blank", "window"}
                if kind in ("main", "tower") and not base:
                    dom.add("arch_win")
                if base and kind != "tower":
                    dom = ({"blank", "window"} | ({"door"} if ground_out else set()))
            if len(pn["segs"]) < 2:
                dom = {"blank"}
        elif kind == "wall":
            dom = {"wall_plain", "slit"} if cell(v, h + 1) is not None else {"wall_plain"}
        elif kind == "plinth":
            dom = {"stone_plain", "stone_band"}
        else:
            if st.rock_detail < 0.35:   # 崖面平整（实测）：只用整块岩面，不加浪蚀凹槽、层缝、裂隙、岩檐
                dom = {"massive"}
            elif sea is not None and h == sea:
                dom = {"notch", "strata"}
            elif sea is not None and h < sea:
                dom = {"strata"}
            else:
                dom = {"strata", "crack"} | ({"massive"} if st.rock_style == "columnar" else set()) | \
                    ({"ledge"} if cell(v, h + 1) is not None else set())
        domains[pk] = dom
        if (v, w, h + 1) in panels:
            rels.append((pk, (v, w, h + 1), "v"))
    # 四边形中心处：相邻墙线的关系（凸角 c / 直线延续 h）
    for q, vs in enumerate(g.quads):
        for h in {hh for (vv, hh) in occ if vv in vs}:
            o = [cell(x, h) is not None for x in vs]
            nf = len(vs)
            mids = {}
            for j in range(nf):  # 中线 m_j 位于角块 j 与 j+1 之间
                a, b = j, (j + 1) % nf
                if o[a] != o[b]:
                    mids[j] = (vs[a], vs[b], h) if o[a] else (vs[b], vs[a], h)
            for j in range(nf):
                j2 = (j + 1) % nf
                if j in mids and j2 in mids and o[j2]:
                    rels.append((mids[j], mids[j2], "c"))
                if nf == 4:   # 四边形：对边中线直线延续；三角形（hex 网格）没有对边
                    j3 = (j + 2) % 4
                    if j < 2 and j in mids and j3 in mids and (j + 1) % 4 not in mids and (j + 3) % 4 not in mids:
                        rels.append((mids[j], mids[j3], "h"))
    solver = WFC(domains, COLUMNAR_WEIGHTS if st.rock_style == "columnar" else WEIGHTS, ALLOWED, rels, fallback="blank")
    tiles = solver.solve(seed=seed)
    # 3. 墙段几何（面板中点端统一映射到 s=0）
    tile_counts = defaultdict(int)
    for pk, t in tiles.items():
        tile_counts[t] += 1
    for key, s in segs.items():
        q, k, side, h = key
        quarter = g.quarter(q, k)
        if side == "n":
            A, B = quarter[1], quarter[2]      # 中点 → 面心
            flip = False
        else:
            A, B = quarter[3], quarter[2]      # 中点 → 面心（原方向为 面心 → 中点）
            flip = True
        dx, dy = B[0] - A[0], B[1] - A[1]
        ln = math.hypot(dx, dy) or 1e-6
        # 外法线：实心四分之一格指向空格
        nx, ny = (dy / ln, -dx / ln) if not flip else (-dy / ln, dx / ln)
        z0, z1 = st.z(h), st.z(h + 1)
        kind = s["kind"]
        role = WALL_ROLE[kind]
        tile = tiles.get((s["v"], s["w"], h), "blank")
        gid = occ[(s["v"], h)].group

        def M(p, A=A, dx=dx, dy=dy, nx=nx, ny=ny, z0=z0, z1=z1, kind=kind, gid=gid):
            ss, tt, dd = p
            return deform((A[0] + dx * ss + nx * dd, A[1] + dy * ss + ny * dd, z0 + (z1 - z0) * tt), kind, gid)

        dirs = {"n": (nx, ny, 0), "+s": (dx / ln, dy / ln, 0), "-s": (-dx / ln, -dy / ln, 0),
                "+t": (0, 0, 1), "-t": (0, 0, -1)}

        def face(pts, r, want, M=M, dirs=dirs):
            mesh.add(r, [M(p) for p in pts], dirs[want])

        wall_piece(tile, face, role, h, ln, st.level_h)
    # 4. 顶面 / 底面
    tops_by_group = defaultdict(list)
    for (v, h), c in occ.items():
        for q in g.vert_quads[v]:
            k = g.quads[q].index(v)
            if cell(v, h + 1) is None:
                tops_by_group[(c.group, h)].append((q, k, v))
            if h > 0 and cell(v, h - 1) is None:
                _underside(mesh, st, g, q, k, v, h, c, segs, deform)
    for (gid, h), quarters in tops_by_group.items():
        _top(mesh, st, g, gid, h, quarters, deform, seed)
    # 5. 海面、泡沫、崩落石、植被
    if st.sea_z is not None:
        _sea(mesh, st, g, segs, deform)
    props = _props(mesh, st, g, tops_by_group, seed)
    if st.sea_z is not None:
        props["ripple_segments"] = _ripples(mesh, st, g)
    props["waterfall_drops"] = _waterfalls(mesh, st, g, deform, seed)
    props["crystals"] = _crystals(mesh, st, g)
    props["trees"] = _trees(mesh, st, g, tops_by_group, seed)
    props["rail_pylons"] = _rails(mesh, st, g)
    props["ramps"] = _stair_ramps(mesh, st, g)
    return {"parts": mesh.as_dict(), "tiles": dict(tile_counts), "wfc": solver.stats, "props": props,
            "cells": len(occ), "segments": len(segs), "panels": len(panels), **walk_data(st, g)}


def _underside(mesh, st, g, q, k, v, h, c, segs, deform):
    """悬空格的底面；下方是门洞（gate）时做成筒拱。"""
    quarter = g.quarter(q, k)
    z0 = st.z(h)
    if (v, h - 1) in st.gates:
        # 拱：离门洞两侧墙越近越低（拱脚），门洞中线处最高
        side_pts = []
        for (q2, k2, side, h2) in segs:
            if h2 == h - 1 and st.cells.get((g.quads[q2][k2], h2)) is not None:
                qq = g.quarter(q2, k2)
                side_pts.append(((qq[1] if side == "n" else qq[2]), (qq[2] if side == "n" else qq[3])))
        n = 4
        grid_pts = [[_bilerp(quarter, i / n, j / n) for j in range(n + 1)] for i in range(n + 1)]
        from .program import _seg_dist
        span = st.arch_span.get((v, h))
        if span:   # 桥拱：半圆（半跨 = 墩间净距的一半），拱脚落在桥墩侧面
            w, rise = span, min(span, st.level_h * (h - 0.3))

            def zf(p):
                d = min((_seg_dist(p, a, b) for a, b in side_pts), default=w)
                u = 1 - min(1.0, d / w)
                return z0 - rise * (1 - math.sqrt(max(0.0, 1 - u * u)))
        else:
            w = st.grid.cell * 0.9
            rise = st.level_h * 0.55

            def zf(p):
                d = min((_seg_dist(p, a, b) for a, b in side_pts), default=w)
                return z0 - rise * (1 - min(1.0, d / w)) ** 2

        for i in range(n):
            for j in range(n):
                ps = [grid_pts[i][j], grid_pts[i + 1][j], grid_pts[i + 1][j + 1], grid_pts[i][j + 1]]
                mesh.add(WALL_ROLE[c.kind], [deform((p[0], p[1], zf(p)), c.kind, c.group) for p in ps], (0, 0, -1))
        return
    mesh.add(WALL_ROLE[c.kind], [deform((p[0], p[1], z0), c.kind, c.group) for p in quarter], (0, 0, -1))


def _bilerp(quad, u, w):
    a, b, c, d = quad
    x0 = (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)
    x1 = (d[0] + (c[0] - d[0]) * u, d[1] + (c[1] - d[1]) * u)
    return (x0[0] + (x1[0] - x0[0]) * w, x0[1] + (x1[1] - x0[1]) * w)


def _top(mesh, st, g, gid, h, quarters, deform, seed):
    from .program import _seg_dist
    kind = st.group_kind.get(gid, "rock")
    roof = st.group_roof.get(gid, "flat")
    z1 = st.z(h + 1)
    region = {(q, k) for q, k, v in quarters}
    # 区域边界：四分之一格朝向「不属于本屋面」的中线
    bnd, eaves = [], []
    for q, k, v in quarters:
        vs = g.quads[q]
        qu = g.quarter(q, k)
        for side, k2 in (("n", (k + 1) % len(vs)), ("p", (k - 1) % len(vs))):
            if (q, k2) not in region:
                seg = (qu[1], qu[2]) if side == "n" else (qu[2], qu[3])   # 逆时针方向，外法线在右侧
                bnd.append(seg)
                if st.cells.get((vs[k2], h)) is None:
                    eaves.append(seg)
    cells_kind = {st.cells[(v, h)].kind for q, k, v in quarters}
    rock_like = "rock" in cells_kind
    if roof == "terrace" or rock_like:
        cols = {v for q, k, v in quarters}
        foot = len({v for (v, hh), c in st.cells.items() if c.group == gid})
        role = "grass" if len(cols) >= max(2, 0.2 * foot) and gid not in st.bare_groups else "rock_top"
        grassy = {}
        for q, k, v in quarters:
            r = role
            if (st.style == "mediterranean" and role == "grass" and gid not in st.measured_groups
                    and _patch(st, *g.verts[v]) < -0.05):
                r = "rock_top"
            if r == "grass":   # 地表色斑：低频噪声量化成 3 个明度阶（漩涡状团块，x.com/OskSta 2023-09-18）
                qu = g.quarter(q, k)
                r = _blotch(st, sum(p[0] for p in qu) / 4, sum(p[1] for p in qu) / 4)
            grassy[(q, k)] = r.startswith("grass")
            mesh.add(r, [deform((p[0], p[1], z1), "rock", gid) for p in g.quarter(q, k)], (0, 0, 1))
        if role == "grass":
            _grass_edges(mesh, st, g, quarters, h, z1, gid, deform, grassy, seed)
        return
    if roof in ("flat", "garden", "crenel", "stair", "deck"):
        role = "grass" if roof == "garden" else "stone_alt"
        for q, k, v in quarters:
            mesh.add(role, [(p[0], p[1], z1) for p in g.quarter(q, k)], (0, 0, 1))
        if roof == "crenel":
            _merlons(mesh, st, g, quarters, h)
        elif roof in ("flat", "garden", "deck"):
            _merlons(mesh, st, g, quarters, h, parapet=True)   # 平顶 / 台地 / 桥面外沿：矮栏杆
        return
    # 坡屋顶 / 穹顶 / 尖锥：高度场 = f(到区域边界的距离)
    pts_all = [p for q, k, v in quarters for p in g.quarter(q, k)]
    cx = sum(p[0] for p in pts_all) / len(pts_all)
    cy = sum(p[1] for p in pts_all) / len(pts_all)
    R = max(math.hypot(p[0] - cx, p[1] - cy) for p in pts_all) or 1.0
    def bdist(p):
        return min((_seg_dist(p, a, b) for a, b in bnd), default=0.0)
    dmax = max((bdist(p) for p in pts_all), default=1.0) or 1.0
    if roof == "dome":
        n = 5
        drum = st.level_h * 0.35
        dome_h = dmax * 1.25   # 略高于半球（文艺复兴式穹顶）

        def zf(p):  # 圆弧剖面：边界处落在鼓座顶，离边界最远处为顶点（任意形状屋面都贴合）
            u = min(1.0, bdist(p) / dmax)
            return z1 + drum + dome_h * math.sqrt(max(0.0, 1 - (1 - u) ** 2))
        role = "dome"
        # 鼓座：穹顶下一圈矮墙
        for a, b in bnd:
            mesh.add("plaster", [(a[0], a[1], z1), (b[0], b[1], z1), (b[0], b[1], z1 + st.level_h * 0.35),
                                 (a[0], a[1], z1 + st.level_h * 0.35)],
                     ((b[1] - a[1]), -(b[0] - a[0]), 0))
        # 檐口：鼓座顶一圈外挑线脚
        _eaves(mesh, bnd, z1 + drum, over=0.22, drop=0.0, role="trim")
    elif roof == "cone":
        n = 3
        cone_h = dmax * 3.2

        def zf(p):
            return z1 + cone_h * min(1.0, bdist(p) / dmax)
        role = "roof"
    else:
        n = 3
        slope, cap = 1.1, st.level_h * 2.6

        def zf(p):
            d = min((_seg_dist(p, a, b) for a, b in bnd), default=0.0)
            return z1 + min(cap, slope * d)
        # 屋顶色域（2023-10-11 调色板工具）：主楼用主色，其余在 roof / roof_b / roof_c 三角色域中按组取一个样
        role = "roof" if kind == "main" else ("roof", "roof_b", "roof_c")[(gid * 7 + h) % 3]
    for q, k, v in quarters:
        qu = g.quarter(q, k)
        gp = [[_bilerp(qu, i / n, j / n) for j in range(n + 1)] for i in range(n + 1)]
        for i in range(n):
            for j in range(n):
                ps = [gp[i][j], gp[i + 1][j], gp[i + 1][j + 1], gp[i][j + 1]]
                mesh.add(role, [(p[0], p[1], zf(p)) for p in ps], (0, 0, 1))
    if roof in ("hip", "cone"):
        _eaves(mesh, eaves, z1, role=role if role.startswith("roof") else "roof")
    if roof in ("dome", "cone"):  # 顶饰：立在最高点
        apex = max(pts_all, key=bdist)
        top = z1 + (drum + dome_h if roof == "dome" else dmax * 3.2)
        if roof == "dome":
            _lantern(mesh, apex[0], apex[1], top - 0.15, max(0.3, min(0.9, dmax * 0.28)))
        else:
            _box(mesh, "trim", apex[0], apex[1], top - 0.05, 0.12, 0.12, 0.9)


_BLOTCH = {}


def _blotch(st, x, y) -> str:
    key = st.grid.seed
    if key not in _BLOTCH:
        _BLOTCH[key] = Noise(key * 7 + 29, (0.16, 0.34), 3)
    n = _BLOTCH[key](x, y, 0.0)[0] * 1.7
    return "grass_dark" if n < -0.3 else "grass_alt" if n > 0.32 else "grass"


def _grass_edges(mesh, st, g, quarters, h, z1, gid, deform, grassy, seed):
    """台缘：草皮唇边越过崖缘下垂（2023-08-30“修改边缘”），内侧一条连续灌丛带（2023-09-29 边缘灌丛）。
    只在草地区域、朝向空格（崖缘）的边上生成；通路格不种灌丛。"""
    rng = random.Random(seed * 131 + gid * 7 + h)
    path_verts = {v for p in st.paths for v in p["verts"]}
    for q, k, v in quarters:
        if not grassy.get((q, k)):
            continue
        vs = g.quads[q]
        qu = g.quarter(q, k)
        for side, nb in (("n", vs[(k + 1) % len(vs)]), ("p", vs[(k - 1) % len(vs)])):
            if st.cells.get((nb, h)) is not None:
                continue
            a, b = (qu[1], qu[2]) if side == "n" else (qu[2], qu[3])
            ln = math.dist(a, b) or 1e-6
            nx, ny = (b[1] - a[1]) / ln, -(b[0] - a[0]) / ln
            pa, pb = deform((a[0], a[1], z1), "rock", gid), deform((b[0], b[1], z1), "rock", gid)
            out, drop = 0.3, 0.55
            # 唇边：向外 0.3 米、向下 0.55 米的斜面，两端略收（圆润的波浪边）
            mesh.add("grass", [pa, pb, (pb[0] + nx * out * 0.8, pb[1] + ny * out * 0.8, pb[2] - drop),
                               (pa[0] + nx * out, pa[1] + ny * out, pa[2] - drop * 1.1)], (nx, ny, 0.6))
            if v in path_verts or nb in path_verts or st.vegetation == "none":   # none：植被另有资产，台缘不种灌丛
                continue
            steps = max(1, int(ln / 0.95))
            for i in range(steps):
                if rng.random() > 0.5:
                    continue
                t = (i + 0.5) / steps
                x = a[0] + (b[0] - a[0]) * t - nx * 0.42
                y = a[1] + (b[1] - a[1]) * t - ny * 0.42
                c = deform((x, y, z1), "rock", gid)
                r = rng.uniform(0.26, 0.42)
                _blob(mesh, "shrub", c[0], c[1], c[2] + r * 0.35, r, r * 0.55, rng, sides=6)


def _eaves(mesh, segs, z1, over=0.42, drop=0.3, role="roof"):
    """出檐：沿外墙向外伸出并下垂，外沿加封檐板；凸角处补三角。role="trim" 时为鼓座檐口线脚。"""
    corners = defaultdict(list)
    for a, b in segs:
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy) or 1e-6
        nx, ny = dy / ln, -dx / ln
        oa = (a[0] + nx * over, a[1] + ny * over, z1 - drop)
        ob = (b[0] + nx * over, b[1] + ny * over, z1 - drop)
        mesh.add(role, [(a[0], a[1], z1), (b[0], b[1], z1), ob, oa], (nx * 0.6, ny * 0.6, 1))
        mesh.add("trim", [oa, ob, (ob[0], ob[1], ob[2] - 0.12), (oa[0], oa[1], oa[2] - 0.12)], (nx, ny, 0))
        mesh.add("trim", [(a[0], a[1], z1 - 0.02), (b[0], b[1], z1 - 0.02), (ob[0], ob[1], ob[2] - 0.12),
                          (oa[0], oa[1], oa[2] - 0.12)], (0, 0, -1))
        for p, o in ((a, oa), (b, ob)):
            corners[(round(p[0], 3), round(p[1], 3))].append((p, o))
    for key, lst in corners.items():
        if len(lst) == 2:
            p = lst[0][0]
            o1, o2 = lst[0][1], lst[1][1]
            mesh.add(role, [(p[0], p[1], z1), o1, o2], (0, 0, 1))
            mesh.add("trim", [o1, o2, (o2[0], o2[1], o2[2] - 0.12), (o1[0], o1[1], o1[2] - 0.12)],
                     ((o1[0] + o2[0]) / 2 - p[0], (o1[1] + o2[1]) / 2 - p[1], 0))


def _merlons(mesh, st, g, quarters, h, parapet=False):
    """城垛：顶层朝外的墙面上方加矮女墙 + 垛口；parapet=True 时只做一道矮栏杆（平顶 / 台地外沿）。
    朝向通路或门洞的一侧不加（不堵路）。"""
    z1 = st.z(h + 1)
    path_verts = {v for p in st.paths for v in p["verts"]}
    for q, k, v in quarters:
        qu = g.quarter(q, k)
        vs = g.quads[q]
        for side, nb in (("n", vs[(k + 1) % len(vs)]), ("p", vs[(k - 1) % len(vs)])):
            if st.cells.get((nb, h)) is not None:
                continue
            if nb in path_verts or v in path_verts or (nb, h) in st.gates or (nb, h + 1) in st.gates:
                continue
            A, B = (qu[1], qu[2]) if side == "n" else (qu[2], qu[3])
            dx, dy = B[0] - A[0], B[1] - A[1]
            ln = math.hypot(dx, dy) or 1e-6
            nx, ny = dy / ln, -dx / ln
            th = 0.35

            def P(s, dd, z):
                return (A[0] + dx * s + nx * dd, A[1] + dy * s + ny * dd, z)
            for s0, s1, hgt in (((0, 1, 0.62),) if parapet else ((0, 1, 0.35), (0.22, 0.72, 1.0))):
                zt = z1 + hgt
                mesh.add("stone", [P(s0, 0, z1), P(s1, 0, z1), P(s1, 0, zt), P(s0, 0, zt)], (nx, ny, 0))
                mesh.add("stone", [P(s0, -th, z1), P(s1, -th, z1), P(s1, -th, zt), P(s0, -th, zt)], (-nx, -ny, 0))
                mesh.add("stone_alt", [P(s0, 0, zt), P(s1, 0, zt), P(s1, -th, zt), P(s0, -th, zt)], (0, 0, 1))
                mesh.add("stone", [P(s0, 0, z1), P(s0, -th, z1), P(s0, -th, zt), P(s0, 0, zt)], (-dx, -dy, 0))
                mesh.add("stone", [P(s1, 0, z1), P(s1, -th, z1), P(s1, -th, zt), P(s1, 0, zt)], (dx, dy, 0))


def _box(mesh, role, cx, cy, z0, sx, sy, sz):
    x0, x1, y0, y1, z1 = cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2, z0 + sz
    mesh.add(role, [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], (0, 0, 1))
    for (a, b, w) in (((x0, y0), (x1, y0), (0, -1, 0)), ((x1, y0), (x1, y1), (1, 0, 0)),
                      ((x1, y1), (x0, y1), (0, 1, 0)), ((x0, y1), (x0, y0), (-1, 0, 0))):
        mesh.add(role, [(a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1)], w)


def _ripples(mesh, st, g, step: float = 0.5, sigma: float = 1.7) -> int:
    """岸外波纹：在水线处陆地的平滑覆盖场上取等值线（marching squares），得到与岸线平行、连续圆滑的 1–2 圈细线；
    沿线用低频噪声留出长间断。参考 x.com/OskSta 2024-04-26 材质测试、2023-06-19 软硬之分（岛外白色波纹线）。"""
    lvl = st.sea_level
    land = [g.verts[v] for (v, h) in st.cells if h == lvl]
    if not land:
        return 0
    z = st.sea_z + 0.02
    pad = 7.0
    x0, y0 = min(p[0] for p in land) - pad, min(p[1] for p in land) - pad
    nx = int((max(p[0] for p in land) + pad - x0) / step) + 1
    ny = int((max(p[1] for p in land) + pad - y0) / step) + 1
    inv = 1 / (2 * sigma * sigma)
    bound = [g.verts[v] for v in range(len(g.verts)) if g.boundary[v]]
    cx = sum(p[0] for p in bound) / len(bound)
    cy = sum(p[1] for p in bound) / len(bound)
    rmax = min(math.hypot(p[0] - cx, p[1] - cy) for p in bound) - 1.0   # 波纹不出海盘
    buckets = defaultdict(list)
    for p in land:
        buckets[(int((p[0] - x0) // 4), int((p[1] - y0) // 4))].append(p)
    field = []
    for j in range(ny):
        y = y0 + j * step
        row = []
        for i in range(nx):
            x = x0 + i * step
            bi, bj = int((x - x0) // 4), int((y - y0) // 4)
            sv = 0.0
            for di in (-2, -1, 0, 1, 2):
                for dj in (-2, -1, 0, 1, 2):
                    for px, py in buckets.get((bi + di, bj + dj), ()):
                        sv += math.exp(-((px - x) ** 2 + (py - y) ** 2) * inv)
            row.append(sv)
        field.append(row)
    gap = Noise(st.grid.seed * 19 + 5, (0.25, 0.45), 2)
    n = 0
    for iso, w in ((0.05, 0.17), (0.006, 0.12)):     # 约离岸 2 米与 3.5 米
        for j in range(ny - 1):
            for i in range(nx - 1):
                c = [field[j][i], field[j][i + 1], field[j + 1][i + 1], field[j + 1][i]]
                corners = [(x0 + i * step, y0 + j * step), (x0 + (i + 1) * step, y0 + j * step),
                           (x0 + (i + 1) * step, y0 + (j + 1) * step), (x0 + i * step, y0 + (j + 1) * step)]
                pts = []
                for a in range(4):
                    b = (a + 1) % 4
                    if (c[a] >= iso) != (c[b] >= iso):
                        t = (iso - c[a]) / (c[b] - c[a])
                        pts.append((corners[a][0] + (corners[b][0] - corners[a][0]) * t,
                                    corners[a][1] + (corners[b][1] - corners[a][1]) * t))
                for k in range(0, len(pts) - 1, 2):
                    p0, p1 = pts[k], pts[k + 1]
                    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
                    if math.hypot(mx - cx, my - cy) > rmax or gap(mx, my, iso * 40)[0] < -0.22:
                        continue
                    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
                    ln = math.hypot(dx, dy) or 1e-6
                    ox, oy = -dy / ln * w / 2, dx / ln * w / 2
                    mesh.add("ripple", [(p0[0] - ox, p0[1] - oy, z), (p1[0] - ox, p1[1] - oy, z),
                                        (p1[0] + ox, p1[1] + oy, z), (p0[0] + ox, p0[1] + oy, z)], (0, 0, 1))
                    n += 1
    return n


def _sea(mesh, st, g, segs, deform):
    z = st.sea_z
    # 海面：外边界顶点的凸包（六边形）
    bpts = [g.verts[v] for v in range(len(g.verts)) if g.boundary[v]]
    cx = sum(p[0] for p in bpts) / len(bpts)
    cy = sum(p[1] for p in bpts) / len(bpts)
    bpts.sort(key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    for a, b in zip(bpts, bpts[1:] + bpts[:1]) if st.sea_plate else ():   # 关掉海面板时只留浪花裙边
        mesh.add("sea", [(cx, cy, z), (a[0], a[1], z), (b[0], b[1], z)], (0, 0, 1))
        # 水体侧壁：盆景式截面，挡住边缘处桥墩、岩脚的水下部分
        mesh.add("sea_deep", [(a[0], a[1], z), (b[0], b[1], z), (b[0], b[1], st.base_z - 0.2), (a[0], a[1], st.base_z - 0.2)],
                 (a[0] + b[0] - 2 * cx, a[1] + b[1] - 2 * cy, 0))
    # 泡沫裙边：水线所在层的岩壁外侧一圈（Bad North：翻转网格 + 泡沫裙边）
    for (q, k, side, h), s in segs.items():
        if s["kind"] != "rock" or not (st.z(h) <= z < st.z(h + 1)):
            continue
        qu = g.quarter(q, k)
        A, B = (qu[1], qu[2]) if side == "n" else (qu[2], qu[3])
        dx, dy = B[0] - A[0], B[1] - A[1]
        ln = math.hypot(dx, dy) or 1e-6
        nx, ny = dy / ln, -dx / ln
        gid = st.cells[(s["v"], h)].group
        pa, pb = deform((A[0], A[1], z), "rock", gid), deform((B[0], B[1], z), "rock", gid)
        w = 0.9
        mesh.add("foam", [(pa[0] - nx * 0.3, pa[1] - ny * 0.3, z + 0.03), (pb[0] - nx * 0.3, pb[1] - ny * 0.3, z + 0.03),
                          (pb[0] + nx * w, pb[1] + ny * w, z + 0.03), (pa[0] + nx * w, pa[1] + ny * w, z + 0.03)],
                 (0, 0, 1))



def _blob(mesh, role, cx, cy, cz, rx, rz, rng, sides=7):
    """低多边形团块（灌木 / 崩落石）：上下两圈 + 顶底点。"""
    rings = []
    for zf, rf in ((-0.55, 0.75), (0.1, 1.0), (0.65, 0.62)):
        ring = []
        for i in range(sides):
            a = 2 * math.pi * i / sides + rng.uniform(-0.2, 0.2)
            r = rx * rf * rng.uniform(0.82, 1.12)
            ring.append((cx + r * math.cos(a), cy + r * math.sin(a), cz + rz * zf))
        rings.append(ring)
    bot, top = (cx, cy, cz - rz * 0.9), (cx, cy, cz + rz)
    for r0, r1 in zip(rings, rings[1:]):
        for i in range(sides):
            j = (i + 1) % sides
            mid = ((r0[i][0] + r1[j][0]) / 2 - cx, (r0[i][1] + r1[j][1]) / 2 - cy, 0)
            mesh.add(role, [r0[i], r0[j], r1[j], r1[i]], mid)
    for i in range(sides):
        j = (i + 1) % sides
        mesh.add(role, [rings[-1][i], rings[-1][j], top], (0, 0, 1))
        mesh.add(role, [rings[0][j], rings[0][i], bot], (0, 0, -1))


def _props(mesh, st, g, tops_by_group, seed) -> dict:
    rng = random.Random(seed * 17 + 3)
    placed = []
    counts = defaultdict(int)

    def free(x, y, r):
        return all(math.hypot(x - px, y - py) >= r + pr for px, py, pr in placed)

    med = st.style == "mediterranean"
    path_verts = {v for p in st.paths for v in p["verts"]}
    if med:  # 成排柏树：台地（台基平顶、崖顶）外沿向内 0.7 米，间距 ≥ 2.8 米，随机留空
        for (gid, h), quarters in sorted(tops_by_group.items()):
            kind, roof = st.group_kind.get(gid, "rock"), st.group_roof.get(gid)
            if not ((kind == "plinth" and roof in ("flat", "garden")) or (kind == "rock" and roof == "cliff")):
                continue
            z1 = st.z(h + 1)
            for q, k, v in quarters:
                if v in path_verts:
                    continue
                vs = g.quads[q]
                qu = g.quarter(q, k)
                for side, nb in (("n", vs[(k + 1) % len(vs)]), ("p", vs[(k - 1) % len(vs)])):
                    if st.cells.get((nb, h)) is not None or nb in path_verts:
                        continue
                    A, B = (qu[1], qu[2]) if side == "n" else (qu[2], qu[3])
                    ln = math.dist(A, B) or 1e-6
                    nx, ny = (B[1] - A[1]) / ln, -(B[0] - A[0]) / ln
                    x = (A[0] + B[0]) / 2 - nx * 0.7
                    y = (A[1] + B[1]) / 2 - ny * 0.7
                    if rng.random() < 0.62 and free(x, y, 1.4):
                        _cypress(mesh, x, y, z1, rng, tall=True)
                        placed.append((x, y, 1.4))
                        counts["cypress"] += 1
    for (gid, h), quarters in sorted(tops_by_group.items()):
        kind = st.group_kind.get(gid, "rock")
        z1 = st.z(h + 1)
        for q, k, v in quarters:
            qu = g.quarter(q, k)
            cx = sum(p[0] for p in qu) / 4
            cy = sum(p[1] for p in qu) / 4
            if v in path_verts:
                continue
            # 植被成簇而不是满地碎点：台缘已有灌丛带，台面内部只在成片区域稀疏点缀
            if kind == "rock" and st.vegetation != "none" and rng.random() < ((0.3 if _patch(st, cx, cy) > 0.05 else 0.03) if med else 0.28):
                r = rng.uniform(0.35, 0.6)
                if free(cx, cy, r):
                    _blob(mesh, "shrub", cx, cy, z1 + r * 0.45, r, r * 0.7, rng)
                    placed.append((cx, cy, r))
                    counts["shrub"] += 1
            elif kind == "plinth" and st.group_roof.get(gid) in ("flat", "garden") and rng.random() < (0.04 if med else 0.12):
                if free(cx, cy, 0.6):
                    _cypress(mesh, cx, cy, z1, rng)
                    placed.append((cx, cy, 0.6))
                    counts["cypress"] += 1
    # 崩落石：海面附近岩脚外侧
    if st.sea_level is not None:
        z = st.sea_z
        for (v, h), c in list(st.cells.items()):
            if c.kind != "rock" or h != st.sea_level - 1:
                continue
            for w in g.neighbors[v]:
                if (w, h) in st.cells or rng.random() > 0.3:
                    continue
                x, y = g.verts[w]
                r = rng.uniform(0.35, 0.8)
                if free(x, y, r):
                    _blob(mesh, "rock_dark", x, y, z + r * 0.1, r, r * 0.8, rng, sides=6)
                    placed.append((x, y, r))
                    counts["boulder"] += 1
    return dict(counts)


def _cypress(mesh, cx, cy, z0, rng, tall=False):
    """意大利柏：细长纺锤形（高 ≈ 8 × 直径），底部一小段树干。"""
    hgt = rng.uniform(6.0, 9.0) if tall else rng.uniform(3.2, 4.6)
    r = hgt * (0.075 if tall else 0.13)
    sides = 7
    rings = []
    for zf, rf in ((0.1, 0.8), (0.32, 1.0), (0.62, 0.78)):
        rings.append([(cx + r * rf * math.cos(2 * math.pi * i / sides), cy + r * rf * math.sin(2 * math.pi * i / sides),
                       z0 + hgt * zf) for i in range(sides)])
    top = (cx, cy, z0 + hgt)
    for r0, r1 in zip(rings, rings[1:]):
        for i in range(sides):
            j = (i + 1) % sides
            mesh.add("cypress", [r0[i], r0[j], r1[j], r1[i]], (r0[i][0] - cx, r0[i][1] - cy, 0))
    for i in range(sides):
        j = (i + 1) % sides
        mesh.add("cypress", [rings[-1][i], rings[-1][j], top], (rings[-1][i][0] - cx, rings[-1][i][1] - cy, 0.3))
        mesh.add("cypress", [rings[0][j], rings[0][i], (cx, cy, z0 + hgt * 0.06)], (0, 0, -1))
    _box(mesh, "door", cx, cy, z0, 0.15, 0.15, hgt * 0.12)


_PATCH = {}


def _patch(st, x, y) -> float:
    """植被成片的低频噪声（-1..1 附近），按网格种子确定。"""
    key = st.grid.seed
    if key not in _PATCH:
        _PATCH[key] = Noise(key * 3 + 101, (0.1, 0.22), 3)
    return _PATCH[key](x, y, 0.0)[0] * 1.6


def _lantern(mesh, cx, cy, z0, r):
    """穹顶采光亭：八角小鼓座 + 小穹顶 + 尖顶饰。"""
    sides = 8
    hgt = r * 1.6
    ring = [(cx + r * math.cos(2 * math.pi * i / sides), cy + r * math.sin(2 * math.pi * i / sides)) for i in range(sides)]
    for i in range(sides):
        a, b = ring[i], ring[(i + 1) % sides]
        mesh.add("plaster", [(a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z0 + hgt), (a[0], a[1], z0 + hgt)],
                 ((a[0] + b[0]) / 2 - cx, (a[1] + b[1]) / 2 - cy, 0))
        # 每面一个深色竖窗（贴面）
        m0 = (a[0] * 0.7 + b[0] * 0.3, a[1] * 0.7 + b[1] * 0.3)
        m1 = (a[0] * 0.3 + b[0] * 0.7, a[1] * 0.3 + b[1] * 0.7)
        off = (((a[0] + b[0]) / 2 - cx) * 0.04, ((a[1] + b[1]) / 2 - cy) * 0.04)
        mesh.add("window", [(m0[0] + off[0], m0[1] + off[1], z0 + hgt * 0.25), (m1[0] + off[0], m1[1] + off[1], z0 + hgt * 0.25),
                            (m1[0] + off[0], m1[1] + off[1], z0 + hgt * 0.8), (m0[0] + off[0], m0[1] + off[1], z0 + hgt * 0.8)],
                 ((a[0] + b[0]) / 2 - cx, (a[1] + b[1]) / 2 - cy, 0))
    prev = [(p[0], p[1], z0 + hgt) for p in ring]
    for k in range(1, 4):
        f = math.cos(k / 4 * math.pi / 2)
        cur = [(cx + (p[0] - cx) * f * 1.08, cy + (p[1] - cy) * f * 1.08, z0 + hgt + r * 1.1 * math.sin(k / 4 * math.pi / 2))
               for p in ring]
        for i in range(sides):
            j = (i + 1) % sides
            mesh.add("dome", [prev[i], prev[j], cur[j], cur[i]], (prev[i][0] - cx, prev[i][1] - cy, 0.5))
        prev = cur
    apex = (cx, cy, z0 + hgt + r * 1.1)
    for i in range(sides):
        mesh.add("dome", [prev[i], prev[(i + 1) % sides], apex], (0, 0, 1))
    _box(mesh, "trim", cx, cy, apex[2] - 0.05, 0.1, 0.1, r * 1.2)


STREAKS = ((-0.5, -0.22, "water"), (-0.22, -0.12, "foam"), (-0.12, 0.14, "water"), (0.14, 0.24, "foam"),
           (0.24, 0.5, "water"))


def _waterfalls(mesh, st, g, deform, seed) -> int:
    """瀑布：从起点沿方向流过阶梯状台地——台面上是一段水带，落差处是一片贴崖水幕（与岩体同一形变，贴合岩面），
    每级落点加泡沫，入海处加一团泡沫。返回落差数。"""
    if not st.waterfalls:
        return 0
    tops = {}
    for (v, h), c in st.cells.items():
        if c.kind in BUILDING_KINDS or c.kind == "wall":   # 水从建筑与城墙脚下流出
            continue
        if h + 1 > tops.get(v, (0,))[0]:
            tops[v] = (h + 1, c.kind, c.group)
    V = g.verts
    rng = random.Random(seed * 29 + 5)
    sea_z = st.sea_z if st.sea_z is not None else st.base_z

    def col(p):
        v = min(range(len(V)), key=lambda i: (V[i][0] - p[0]) ** 2 + (V[i][1] - p[1]) ** 2)
        return tops.get(v)

    drops = 0
    for wf in st.waterfalls:
        (x, y), (dx, dy), w = wf["at"], wf["dir"], wf["w"]
        px, py = -dy, dx
        cur = col((x, y))
        if cur is None:
            continue
        s, step, start = 0.0, 0.3, (x, y)
        prof = []
        while s < 80:
            s += step
            c = col((x + dx * s, y + dy * s))
            if c is not None and c[0] >= cur[0]:
                continue                                  # 同高或小凸起：水在原高度继续流（视作冲出的沟槽）
            edge = (x + dx * (s - step / 2), y + dy * (s - step / 2))
            prof.append(("flat", start, edge, cur))
            prof.append(("drop", edge, cur, c))
            if c is None:
                break
            cur, start = c, edge

        def D(pt, info):
            return deform(pt, info[1], info[2])

        for item in prof:
            if item[0] == "flat":
                _, a, b, info = item
                z = st.z(info[0]) + 0.06
                n = max(1, int(math.dist(a, b) / 0.8))
                for i in range(n):
                    p0 = (a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)
                    p1 = (a[0] + (b[0] - a[0]) * (i + 1) / n, a[1] + (b[1] - a[1]) * (i + 1) / n)
                    for u0, u1, role in STREAKS:
                        pts = [(p0[0] + px * w * u0, p0[1] + py * w * u0, z), (p1[0] + px * w * u0, p1[1] + py * w * u0, z),
                               (p1[0] + px * w * u1, p1[1] + py * w * u1, z), (p0[0] + px * w * u1, p0[1] + py * w * u1, z)]
                        mesh.add(role, [D(q, info) for q in pts], (0, 0, 1))
            else:
                _, e, info, below = item
                drops += 1
                z_hi = st.z(info[0]) + 0.06
                z_lo = st.z(below[0]) if below is not None else sea_z
                n = max(1, math.ceil((z_hi - z_lo) / 1.0))
                for i in range(n):
                    za, zb = z_hi - (z_hi - z_lo) * i / n, z_hi - (z_hi - z_lo) * (i + 1) / n
                    oa, ob = 0.45 + 0.25 * i / n, 0.45 + 0.25 * (i + 1) / n   # 水幕略向外抛
                    for u0, u1, role in STREAKS:
                        wa, wb = w * (1 + 0.15 * i / n), w * (1 + 0.15 * (i + 1) / n)
                        pts = [(e[0] + dx * oa + px * wa * u0, e[1] + dy * oa + py * wa * u0, za),
                               (e[0] + dx * oa + px * wa * u1, e[1] + dy * oa + py * wa * u1, za),
                               (e[0] + dx * ob + px * wb * u1, e[1] + dy * ob + py * wb * u1, zb),
                               (e[0] + dx * ob + px * wb * u0, e[1] + dy * ob + py * wb * u0, zb)]
                        mesh.add(role, [D(q, info) for q in pts], (dx, dy, 0))
                # 落点泡沫
                f = D((e[0] + dx * 0.9, e[1] + dy * 0.9, z_lo + 0.1), below or info)
                for _ in range(3):
                    _blob(mesh, "foam", f[0] + rng.uniform(-w * 0.35, w * 0.35), f[1] + rng.uniform(-0.3, 0.3),
                          f[2], w * rng.uniform(0.18, 0.28), w * 0.12, rng, sides=6)
    return drops


def _ground_z(st, g, p, buildings=False) -> float:
    """点 p 所在对偶格（最近顶点）的最高实心格顶面高度；没有格子时为海面 / 第 0 层。"""
    V = g.verts
    v = min(range(len(V)), key=lambda i: (V[i][0] - p[0]) ** 2 + (V[i][1] - p[1]) ** 2)
    hs = [h for (vv, h), c in st.cells.items() if vv == v and (buildings or c.kind not in BUILDING_KINDS)]
    if hs:
        return st.z(max(hs) + 1)
    return st.sea_z if st.sea_z is not None else st.z(0)


def _prism(mesh, cx, cy, z0, r, h, tip, tilt, az, roles, sides=6, rot=0.0):
    """倾斜的尖顶棱柱（晶柱）：底面 z0，柱身高 h，尖顶高 tip；tilt 为外倾角（弧度），az 为倾斜方位。"""
    ox, oy = math.sin(tilt) * math.cos(az), math.sin(tilt) * math.sin(az)

    def P(x, y, z):   # 以底面中心为原点，沿倾斜轴上移
        return (cx + x + ox * (z - z0), cy + y + oy * (z - z0), z)
    ring0 = [P(r * math.cos(rot + 2 * math.pi * i / sides), r * math.sin(rot + 2 * math.pi * i / sides), z0) for i in range(sides)]
    ring1 = [P(r * math.cos(rot + 2 * math.pi * i / sides), r * math.sin(rot + 2 * math.pi * i / sides), z0 + h) for i in range(sides)]
    apex = P(0, 0, z0 + h + tip)
    for i in range(sides):
        j = (i + 1) % sides
        role = roles[i % 2]
        out = (math.cos(rot + 2 * math.pi * (i + 0.5) / sides), math.sin(rot + 2 * math.pi * (i + 0.5) / sides), 0)
        mesh.add(role, [ring0[i], ring0[j], ring1[j], ring1[i]], out)
        mesh.add(roles[(i + 1) % 2], [ring1[i], ring1[j], apex], (out[0], out[1], 0.6))


def _crystals(mesh, st, g) -> int:
    """晶柱簇（莫比斯水晶城）：一根六棱主晶柱 + 3–5 根卫星晶柱，尖顶、微外倾，亮暗两色交替成晶面；塔脚金铜平台。"""
    n = 0
    for c in st.crystals:
        rng = random.Random(c["seed"])
        x, y = c["at"]
        z0 = st.z(int(c["bottom"])) if c.get("bottom") is not None else _ground_z(st, g, (x, y), buildings=True)
        H = max(st.z(c["top"]) - z0, 2.0)
        R = max(0.5, c["r"] * 0.42)
        # 金铜平台（八角薄台）
        ring = [(x + R * 1.9 * math.cos(2 * math.pi * i / 8), y + R * 1.9 * math.sin(2 * math.pi * i / 8)) for i in range(8)]
        zt = z0 + 0.35
        mesh.add("metal", [(p[0], p[1], zt) for p in ring], (0, 0, 1))
        for i in range(8):
            a, b = ring[i], ring[(i + 1) % 8]
            mesh.add("metal", [(a[0], a[1], z0 - 0.2), (b[0], b[1], z0 - 0.2), (b[0], b[1], zt), (a[0], a[1], zt)],
                     ((a[0] + b[0]) / 2 - x, (a[1] + b[1]) / 2 - y, 0))
        _prism(mesh, x, y, zt, R, H * 0.8, R * 2.2, rng.uniform(0, 0.05), rng.uniform(0, 6.28),
               ("crystal", "crystal_alt"), rot=rng.uniform(0, 1))
        n += 1
        for k in range(rng.randint(3, 5)):
            az = 2 * math.pi * k / 5 + rng.uniform(-0.4, 0.4)
            d = R * rng.uniform(1.0, 1.5)
            r2 = R * rng.uniform(0.4, 0.7)
            _prism(mesh, x + d * math.cos(az), y + d * math.sin(az), zt, r2, H * rng.uniform(0.3, 0.62), r2 * 2.2,
                   rng.uniform(0.08, 0.2), az, ("crystal_alt", "crystal"), rot=rng.uniform(0, 1))
            n += 1
    return n


def _tree(mesh, x, y, z0, r, h, rng):
    """大冠树：七棱渐细树干 + 3–6 个低多边形树冠团块（巨树加板根）。"""
    tr = max(0.18, r * 0.16)
    th = h * 0.55
    sides = 7
    for i in range(sides):
        a0, a1 = 2 * math.pi * i / sides, 2 * math.pi * (i + 1) / sides
        p = [(x + tr * math.cos(a0), y + tr * math.sin(a0), z0), (x + tr * math.cos(a1), y + tr * math.sin(a1), z0),
             (x + tr * 0.6 * math.cos(a1), y + tr * 0.6 * math.sin(a1), z0 + th),
             (x + tr * 0.6 * math.cos(a0), y + tr * 0.6 * math.sin(a0), z0 + th)]
        mesh.add("trunk", p, (math.cos((a0 + a1) / 2), math.sin((a0 + a1) / 2), 0))
    if r > 3.0:   # 巨树：板根
        for k in range(5):
            a = 2 * math.pi * k / 5 + rng.uniform(-0.3, 0.3)
            _blob(mesh, "trunk", x + tr * 1.3 * math.cos(a), y + tr * 1.3 * math.sin(a), z0 + tr * 0.3, tr * 0.7, tr * 0.6, rng, 5)
    for k in range(rng.randint(3, 6)):
        a = rng.uniform(0, 6.28)
        d = r * rng.uniform(0.1, 0.55)
        rr = r * rng.uniform(0.5, 0.8)
        _blob(mesh, "canopy", x + d * math.cos(a), y + d * math.sin(a), z0 + h * rng.uniform(0.62, 0.82), rr, rr * 0.65, rng)


def _trees(mesh, st, g, tops_by_group, seed) -> int:
    n = 0
    for t in st.trees:
        rng = random.Random(t["seed"])
        x, y = t["at"]
        z0 = st.z(int(t["bottom"])) if t.get("bottom") is not None else _ground_z(st, g, (x, y))
        _tree(mesh, x, y, z0, t["r"], t["h"], rng)
        n += 1
    if st.vegetation == "trees":   # 自动成林：台地草面上稀疏种大冠树（避开通路）
        rng = random.Random(seed * 41 + 9)
        path_verts = {v for p in st.paths for v in p["verts"]}
        placed = [(t["at"][0], t["at"][1], t["r"]) for t in st.trees] + [(c["at"][0], c["at"][1], c["r"]) for c in st.crystals]
        for (gid, h), quarters in sorted(tops_by_group.items()):
            if st.group_kind.get(gid) not in ("rock", "plinth") or st.group_roof.get(gid) in ("stair", "deck"):
                continue
            for q, k, v in quarters:
                if v in path_verts or any(w in path_verts for w in g.neighbors[v]) or rng.random() > 0.12:
                    continue
                qu = g.quarter(q, k)
                cx, cy = sum(p[0] for p in qu) / 4, sum(p[1] for p in qu) / 4
                r = rng.uniform(1.6, 2.6)
                if all(math.hypot(cx - px, cy - py) >= (r + pr) * 0.8 for px, py, pr in placed):
                    _tree(mesh, cx, cy, st.z(h + 1), r, r * rng.uniform(2.4, 3.2), rng)
                    placed.append((cx, cy, r))
                    n += 1
    return n


def _rails(mesh, st, g) -> int:
    """细轨高架：宽 1.1 米、厚 0.45 米的轨道梁沿折线架设，约每 7 米一根细柱落到地面 / 海底；梁两侧金铜细边。"""
    n = 0
    for rl in st.rails:
        pts, z = rl["pts"], rl["z"]
        hw, th = 0.55, 0.45
        for a, b in zip(pts, pts[1:]):
            L = math.dist(a, b)
            if L < 1e-3:
                continue
            dx, dy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            px, py = -dy, dx
            c = [(a[0] + px * hw, a[1] + py * hw), (b[0] + px * hw, b[1] + py * hw),
                 (b[0] - px * hw, b[1] - py * hw), (a[0] - px * hw, a[1] - py * hw)]
            mesh.add("stone_alt", [(q[0], q[1], z) for q in c], (0, 0, 1))
            mesh.add("stone", [(q[0], q[1], z - th) for q in c], (0, 0, -1))
            for (p0, p1), nv in (((c[0], c[1]), (px, py)), ((c[2], c[3]), (-px, -py))):
                mesh.add("metal", [(p0[0], p0[1], z - th), (p1[0], p1[1], z - th), (p1[0], p1[1], z + 0.12), (p0[0], p0[1], z + 0.12)],
                         (nv[0], nv[1], 0))
            k = max(1, round(L / 7.0))
            for i in range(k + 1):
                x, y = a[0] + dx * L * i / k, a[1] + dy * L * i / k
                zb = _ground_z(st, g, (x, y))
                if z - th - zb > 0.5:
                    _box(mesh, "stone", x, y, zb - 0.3, 0.45, 0.45, z - th - zb + 0.3)
                    n += 1
    return n


def _stair_ramps(mesh, st, g) -> int:
    """通路坡道：相邻通路格高一层处，把低格顶面做成一整格长的坡（约 45°，玩家可走），
    深浅条纹表现台阶；坡面高出原顶面的两侧补挡墙。"""
    n = 0
    V = g.verts
    for p in st.paths:
        walk = p.get("walk") or {}
        for v, L in walk.items():
            ups = [w for w in g.neighbors[v] if walk.get(w) == L + 1]
            if not ups:
                continue
            w = ups[0]
            M = ((V[v][0] + V[w][0]) / 2, (V[v][1] + V[w][1]) / 2)
            downs = [u for u in g.neighbors[v] if u in walk and walk[u] <= L and u != w]
            if downs:
                u = max(downs, key=lambda u: -((V[u][0] - V[v][0]) * (M[0] - V[v][0]) + (V[u][1] - V[v][1]) * (M[1] - V[v][1])))
                F = ((V[v][0] + V[u][0]) / 2, (V[v][1] + V[u][1]) / 2)
            else:
                F = (2 * V[v][0] - M[0], 2 * V[v][1] - M[1])
            ax, ay = M[0] - F[0], M[1] - F[1]
            L2 = ax * ax + ay * ay or 1.0
            z0 = st.z(L)

            def hf(q):
                t = max(0.0, min(1.0, ((q[0] - F[0]) * ax + (q[1] - F[1]) * ay) / L2))
                return z0 + st.level_h * t, t
            for qd in g.vert_quads[v]:
                k = g.quads[qd].index(v)
                qu = g.quarter(qd, k)
                m = 3
                gp = [[_bilerp(qu, i / m, j / m) for j in range(m + 1)] for i in range(m + 1)]
                for i in range(m):
                    for j in range(m):
                        ps = [gp[i][j], gp[i + 1][j], gp[i + 1][j + 1], gp[i][j + 1]]
                        tm = hf(((ps[0][0] + ps[2][0]) / 2, (ps[0][1] + ps[2][1]) / 2))[1]
                        role = "stone_alt" if int(tm * 6) % 2 else "stone"
                        mesh.add(role, [(q[0], q[1], hf(q)[0] + 0.02) for q in ps], (0, 0, 1))
                # 挡墙：朝向非通路邻格的四分之一格边
                vs = g.quads[qd]
                for side, nb in (("n", vs[(k + 1) % len(vs)]), ("p", vs[(k - 1) % len(vs)])):
                    if nb in walk:
                        continue
                    a, b = (qu[1], qu[2]) if side == "n" else (qu[2], qu[3])
                    ha, hb = hf(a)[0], hf(b)[0]
                    if max(ha, hb) - z0 < 0.05:
                        continue
                    ln = math.dist(a, b) or 1e-6
                    mesh.add("stone", [(a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], hb + 0.02), (a[0], a[1], ha + 0.02)],
                             ((b[1] - a[1]) / ln, -(b[0] - a[0]) / ln, 0))
            n += 1
    return n


def walk_data(st, g, limit: int = 600) -> dict:
    """给 Godot 的可站立数据（Blender 坐标，Z 向上）：walk = 平台 / 台地 / 通路顶面上的点；path = 各通路按顺序的点。"""
    V = g.verts
    top = {}
    for (v, h), c in st.cells.items():
        if h + 1 > top.get(v, (0, None))[0]:
            top[v] = (h + 1, c.kind)
    ramp = {v for p in st.paths for v, L in (p.get("walk") or {}).items()
            if any((p.get("walk") or {}).get(w) == L + 1 for w in g.neighbors[v])}   # 坡道格不是平地，不作站立点
    walk = [[round(V[v][0], 2), round(V[v][1], 2), round(st.z(t), 2)] for v, (t, kind) in sorted(top.items())
            if kind in ("rock", "plinth", "wall") and v not in ramp and not st.gates.issuperset({(v, t)})]
    if len(walk) > limit:
        walk = walk[:: max(1, len(walk) // limit)]
    paths = []
    for p in st.paths:
        wk = p.get("walk") or {}
        pts = sorted(wk, key=lambda v: (wk[v], V[v][0], V[v][1]))
        paths.append([[round(V[v][0], 2), round(V[v][1], 2), round(st.z(wk[v]), 2)] for v in pts])
    return {"walk": walk, "paths": paths}
