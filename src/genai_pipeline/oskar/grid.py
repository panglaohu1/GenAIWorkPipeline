"""Oskar 式不规则松弛网格（纯 Python，确定性，不依赖 bpy/numpy）。两种基础格：

- hex（默认，Oskar 2024 星球项目）：三角网本身就是网格面，状态存在三角网顶点上，每个顶点周围的
  三角形「角块」拼成它的对偶格——六边形（边翻转处出现少量五边形 / 七边形）。作者原话：
  "Just triangles. Though of course the dual of those is a mix of hexagons and the occasional pentagon and
  heptagon"（x.com/OskSta/status/1768627849529893109）。
- quad（Townscaper 2020，旧场景兼容）：六边形三角点阵 → 随机合并相邻三角形为四边形 → 全部面按
  「角点 + 两条边中点 + 面中心」细分（此后 100% 四边形）→ 迭代松弛使每个四边形趋近正方形。

两种网格共用同一接口：面 quads[q] 是逆时针的 n 边形（hex 为 3，quad 为 4）；面 q 的第 k 个「角块」
quarter(q, k) = 角点 → 下一边中点 → 面中心 → 上一边中点（总是 4 个点）；顶点周围的角块拼成对偶格。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

SQ3 = math.sqrt(3.0)


@dataclass
class Grid:
    verts: list[list[float]]                 # [x, y]
    quads: list[tuple[int, ...]]             # 网格面（逆时针 n 边形：hex 为三角形，quad 为四边形）
    boundary: list[bool]                     # 顶点是否在外边界
    seed: int
    cell: float                              # 对偶格的名义边长（米）：对偶格面积 ≈ cell²
    radius: int                              # 名义半径：场景半宽 ≈ radius × 2 × cell（两种网格一致）
    vert_quads: list[list[int]] = field(default_factory=list)
    neighbors: list[list[int]] = field(default_factory=list)   # 共享网格边的相邻顶点（hex：对偶格的相邻格）
    kind: str = "quad"

    def quad_center(self, q: int) -> tuple[float, float]:
        vs = self.quads[q]
        n = len(vs)
        return (sum(self.verts[i][0] for i in vs) / n, sum(self.verts[i][1] for i in vs) / n)

    def mid(self, i: int, j: int) -> tuple[float, float]:
        a, b = self.verts[i], self.verts[j]
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)

    def quarter(self, q: int, k: int) -> list[tuple[float, float]]:
        """面 q 中属于第 k 个角点的角块（逆时针 4 点）：角点 → 下一边中点 → 面中心 → 上一边中点。"""
        vs = self.quads[q]
        n = len(vs)
        v, nx, pv = vs[k], vs[(k + 1) % n], vs[(k - 1) % n]
        return [tuple(self.verts[v]), self.mid(v, nx), self.quad_center(q), self.mid(pv, v)]

    def cell_sides(self, v: int) -> int:
        """顶点 v 的对偶格是几边形（hex 网格内部：6 为六边形，5 / 7 为翻转缺陷）。"""
        return len(self.neighbors[v])

    def dual_cell(self, v: int) -> list[tuple[float, float]]:
        """顶点 v 的对偶格多边形（按角度排序的边中点与面中心交替点），用于可视化与拾取。"""
        cx, cy = self.verts[v]
        pts = []
        for q in self.vert_quads[v]:
            k = self.quads[q].index(v)
            pts += self.quarter(q, k)[1:]
        uniq = {(round(p[0], 6), round(p[1], 6)): p for p in pts}.values()
        return sorted(uniq, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))


def _area(poly) -> float:
    s = 0.0
    for i in range(len(poly)):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % len(poly)]
        s += x0 * y1 - x1 * y0
    return s / 2


def _angles_ok(poly, lo=math.radians(36), hi=math.radians(162)) -> bool:
    n = len(poly)
    sign = 0
    for i in range(n):
        ax, ay = poly[i - 1]
        bx, by = poly[i]
        cx, cy = poly[(i + 1) % n]
        u, w = (ax - bx, ay - by), (cx - bx, cy - by)
        cross = u[0] * w[1] - u[1] * w[0]
        s = 1 if cross < 0 else -1  # 逆时针多边形内角处 cross<0
        if sign and s != sign:
            return False
        sign = s
        cos = (u[0] * w[0] + u[1] * w[1]) / (math.hypot(*u) * math.hypot(*w) + 1e-12)
        ang = math.acos(max(-1.0, min(1.0, cos)))
        if not lo <= ang <= hi:
            return False
    return True


def make_grid(radius: int = 6, seed: int = 0, cell: float = 2.0, relax_iters: int = 60,
              relax_rate: float = 0.5, kind: str = "quad", flip_rate: float = 0.012) -> Grid:
    """radius：名义半径（场景半宽 ≈ radius × 2 × cell）；cell：对偶格名义边长（米）。
    kind="hex"：三角网 + 六边形对偶格（Oskar 星球项目）；kind="quad"：Townscaper 式全四边形网格。"""
    if kind == "hex":
        return make_hex_grid(radius=radius, seed=seed, cell=cell, flip_rate=flip_rate)
    if kind != "quad":
        raise ValueError("grid.kind 只能是 hex 或 quad")
    rng = random.Random(seed)
    L = cell * 2.0  # 细分一次后边长减半
    # 1. 六边形三角点阵
    idx: dict[tuple[int, int], int] = {}
    pts: list[list[float]] = []
    for q in range(-radius, radius + 1):
        for r in range(-radius, radius + 1):
            if abs(q + r) <= radius:
                idx[(q, r)] = len(pts)
                pts.append([(q + r / 2) * L, r * SQ3 / 2 * L])
    tris = []
    for (q, r), a in idx.items():
        b, c = idx.get((q + 1, r)), idx.get((q, r + 1))
        if b is not None and c is not None:
            tris.append((a, b, c))
        d = idx.get((q - 1, r + 1))
        if c is not None and d is not None:
            tris.append((a, c, d))
    # 2. 随机合并相邻三角形为四边形
    edge_tris: dict[tuple[int, int], list[int]] = {}
    for ti, t in enumerate(tris):
        for i in range(3):
            e = tuple(sorted((t[i], t[(i + 1) % 3])))
            edge_tris.setdefault(e, []).append(ti)
    inner = [e for e, ts in edge_tris.items() if len(ts) == 2]
    rng.shuffle(inner)
    used = [False] * len(tris)
    faces: list[tuple[int, ...]] = []
    for e in inner:
        ta, tb = edge_tris[e]
        if used[ta] or used[tb]:
            continue
        A, B = tris[ta], tris[tb]
        # A 中按逆时针找到 e0→e1
        for i in range(3):
            if {A[i], A[(i + 1) % 3]} == set(e):
                e0, e1, oa = A[i], A[(i + 1) % 3], A[(i + 2) % 3]
                break
        ob = next(v for v in B if v not in e)
        quad = (e0, ob, e1, oa)
        if _angles_ok([pts[v] for v in quad]):
            used[ta] = used[tb] = True
            faces.append(quad)
    faces += [t for ti, t in enumerate(tris) if not used[ti]]
    # 3. 细分：每个面按角点切成小四边形
    verts = [p[:] for p in pts]
    mids: dict[tuple[int, int], int] = {}

    def mid(a, b):
        k = (a, b) if a < b else (b, a)
        if k not in mids:
            mids[k] = len(verts)
            verts.append([(pts[a][0] + pts[b][0]) / 2, (pts[a][1] + pts[b][1]) / 2])
        return mids[k]

    quads: list[tuple[int, int, int, int]] = []
    for f in faces:
        n = len(f)
        c = len(verts)
        verts.append([sum(pts[v][0] for v in f) / n, sum(pts[v][1] for v in f) / n])
        for i in range(n):
            quads.append((f[i], mid(f[i], f[(i + 1) % n]), c, mid(f[i - 1], f[i])))
    # 4. 边界顶点
    ecount: dict[tuple[int, int], int] = {}
    for qd in quads:
        for i in range(4):
            e = tuple(sorted((qd[i], qd[(i + 1) % 4])))
            ecount[e] = ecount.get(e, 0) + 1
    boundary = [False] * len(verts)
    for (a, b), n in ecount.items():
        if n == 1:
            boundary[a] = boundary[b] = True
    # 5. 松弛：每个四边形趋近正方形（闭式求最优旋转），边界固定
    g_r = sum(math.dist(verts[qd[0]], verts[qd[2]]) for qd in quads) / len(quads) / 2
    for _ in range(relax_iters):
        force = [[0.0, 0.0, 0] for _ in verts]
        for qd in quads:
            cx = sum(verts[v][0] for v in qd) / 4
            cy = sum(verts[v][1] for v in qd) / 4
            sx = sy = 0.0
            rr = 0.0
            for i, v in enumerate(qd):  # 把第 i 个角点反向旋转 i·90° 后累加
                px, py = verts[v][0] - cx, verts[v][1] - cy
                rr += math.hypot(px, py)
                a = -i * math.pi / 2
                sx += px * math.cos(a) - py * math.sin(a)
                sy += px * math.sin(a) + py * math.cos(a)
            th = math.atan2(sy, sx)
            r = 0.5 * rr / 4 + 0.5 * g_r
            for i, v in enumerate(qd):
                tx = cx + r * math.cos(th + i * math.pi / 2)
                ty = cy + r * math.sin(th + i * math.pi / 2)
                force[v][0] += tx - verts[v][0]
                force[v][1] += ty - verts[v][1]
                force[v][2] += 1
        for v, (fx, fy, n) in enumerate(force):
            if n and not boundary[v]:
                verts[v][0] += relax_rate * fx / n
                verts[v][1] += relax_rate * fy / n
    return _finish(Grid(verts=verts, quads=quads, boundary=boundary, seed=seed, cell=cell, radius=radius, kind="quad"))


def _finish(g: Grid) -> Grid:
    g.vert_quads = [[] for _ in g.verts]
    nb: list[set[int]] = [set() for _ in g.verts]
    for qi, qd in enumerate(g.quads):
        n = len(qd)
        for i, v in enumerate(qd):
            g.vert_quads[v].append(qi)
            nb[v].add(qd[(i + 1) % n])
            nb[v].add(qd[(i - 1) % n])
    g.neighbors = [sorted(s) for s in nb]
    return g


# 三角点阵间距 / cell：对偶六边形面积 (√3/2)·L² = cell²，与四边形网格的格子一样大
HEX_SPACING = math.sqrt(2.0 / SQ3)


def _tri_area(p, a, b, c) -> float:
    return ((p[b][0] - p[a][0]) * (p[c][1] - p[a][1]) - (p[b][1] - p[a][1]) * (p[c][0] - p[a][0])) / 2


def _edge_map(tris) -> dict[tuple[int, int], list[int]]:
    edges: dict[tuple[int, int], list[int]] = {}
    for ti, t in enumerate(tris):
        for i in range(3):
            edges.setdefault(tuple(sorted((t[i], t[(i + 1) % 3]))), []).append(ti)
    return edges


def _min_angle(p, tri) -> float:
    out = math.pi
    for i in range(3):
        a, b, c = p[tri[i - 1]], p[tri[i]], p[tri[(i + 1) % 3]]
        u, w = (a[0] - b[0], a[1] - b[1]), (c[0] - b[0], c[1] - b[1])
        cos = (u[0] * w[0] + u[1] * w[1]) / (math.hypot(*u) * math.hypot(*w) + 1e-12)
        out = min(out, math.acos(max(-1.0, min(1.0, cos))))
    return out


def make_hex_grid(radius: int = 6, seed: int = 0, cell: float = 2.0, relax_iters: int = 40,
                  relax_rate: float = 0.5, flip_rate: float = 0.012, jitter: float = 0.22) -> Grid:
    """三角网 + 六边形对偶格（Oskar 星球项目的基础格）。

    1. 六边形范围内的三角点阵（间距 L = cell·HEX_SPACING，使对偶六边形面积 = cell²）；
    2. 内部点抖动，再随机翻转少量内部边（翻转后各顶点仍有 5–7 个邻居），对偶格因此出现零星五边形 / 七边形，
       网格不再是规整蜂窝；
    3. 迭代松弛：每个三角形趋近等边（闭式求最优旋转，同 Townscaper 的四边形松弛），外边界固定。"""
    rng = random.Random(seed * 7349 + 11)
    L = cell * HEX_SPACING
    R = max(2, round(radius * 2 * cell / L))
    idx: dict[tuple[int, int], int] = {}
    pts: list[list[float]] = []
    ring: list[int] = []
    for q in range(-R, R + 1):
        for r in range(-R, R + 1):
            if abs(q + r) <= R:
                idx[(q, r)] = len(pts)
                pts.append([(q + r / 2) * L, r * SQ3 / 2 * L])
                ring.append(max(abs(q), abs(r), abs(q + r)))
    tris: list[tuple[int, int, int]] = []
    for (q, r), a in idx.items():
        b, c = idx.get((q + 1, r)), idx.get((q, r + 1))
        if b is not None and c is not None:
            tris.append((a, b, c))
        d = idx.get((q - 1, r + 1))
        if c is not None and d is not None:
            tris.append((a, c, d))
    boundary = [rg == R for rg in ring]
    for v, p in enumerate(pts):
        if not boundary[v]:
            p[0] += rng.uniform(-jitter, jitter) * L
            p[1] += rng.uniform(-jitter, jitter) * L
    # 边翻转：制造 5 / 7 邻的缺陷点（不碰外边界，缺陷之间互不相邻）
    valence = [0] * len(pts)
    for t in tris:
        for v in t:
            valence[v] += 1   # 三角形数；内部点的邻居数 = 三角形数
    edges = _edge_map(tris)
    inner_edges = [e for e, ts in edges.items() if len(ts) == 2 and not boundary[e[0]] and not boundary[e[1]]]
    rng.shuffle(inner_edges)
    want = int(len(inner_edges) * flip_rate)
    touched: set[int] = set()
    flipped = 0
    for a, b in inner_edges:
        if flipped >= want:
            break
        ts = edges.get((a, b))
        if not ts or len(ts) != 2:
            continue
        t1, t2 = tris[ts[0]], tris[ts[1]]
        # 让 t1 含有 a→b（逆时针）
        if not any(t1[i] == a and t1[(i + 1) % 3] == b for i in range(3)):
            t1, t2 = t2, t1
            ts = [ts[1], ts[0]]
        c = next(v for v in t1 if v not in (a, b))
        d = next(v for v in t2 if v not in (a, b))
        quad = {a, b, c, d}
        if quad & touched or any(boundary[v] for v in (c, d)):
            continue
        if valence[a] - 1 < 5 or valence[b] - 1 < 5 or valence[c] + 1 > 7 or valence[d] + 1 > 7:
            continue
        n1, n2 = (a, d, c), (d, b, c)
        if _tri_area(pts, *n1) <= 0 or _tri_area(pts, *n2) <= 0 or \
                min(_min_angle(pts, n1), _min_angle(pts, n2)) < math.radians(28):
            continue
        tris[ts[0]], tris[ts[1]] = n1, n2
        valence[a] -= 1
        valence[b] -= 1
        valence[c] += 1
        valence[d] += 1
        touched |= quad
        for v in quad:   # 缺陷点的邻居也不再参与翻转，五 / 七边形保持零星分布
            touched.update(w for t in tris if v in t for w in t)
        edges = _edge_map(tris)
        flipped += 1
    # 松弛：每个三角形趋近等边（顶点逆时针，依次差 120°）
    g_r = L / SQ3
    step = 2 * math.pi / 3
    for _ in range(relax_iters):
        force = [[0.0, 0.0, 0] for _ in pts]
        for t in tris:
            cx = sum(pts[v][0] for v in t) / 3
            cy = sum(pts[v][1] for v in t) / 3
            sx = sy = rr = 0.0
            for i, v in enumerate(t):
                px, py = pts[v][0] - cx, pts[v][1] - cy
                rr += math.hypot(px, py)
                a = -i * step
                sx += px * math.cos(a) - py * math.sin(a)
                sy += px * math.sin(a) + py * math.cos(a)
            th = math.atan2(sy, sx)
            r = 0.5 * rr / 3 + 0.5 * g_r
            for i, v in enumerate(t):
                force[v][0] += cx + r * math.cos(th + i * step) - pts[v][0]
                force[v][1] += cy + r * math.sin(th + i * step) - pts[v][1]
                force[v][2] += 1
        for v, (fx, fy, n) in enumerate(force):
            if n and not boundary[v]:
                pts[v][0] += relax_rate * fx / n
                pts[v][1] += relax_rate * fy / n
    g = _finish(Grid(verts=pts, quads=list(tris), boundary=boundary, seed=seed, cell=cell, radius=radius, kind="hex"))
    g.flips = flipped
    return g


def grid_stats(g: Grid) -> dict:
    areas = [_area([g.verts[v] for v in qd]) for qd in g.quads]
    lo, hi = (math.radians(30), math.radians(150)) if g.kind == "quad" else (math.radians(25), math.radians(120))
    out = {"kind": g.kind, "verts": len(g.verts), "quads": len(g.quads), "inverted": sum(1 for a in areas if a <= 0),
           "area_min": round(min(areas), 3), "area_max": round(max(areas), 3),
           "angles_bad": sum(1 for qd in g.quads if not _angles_ok([g.verts[v] for v in qd], lo, hi))}
    if g.kind == "hex":
        sides: dict[int, int] = {}
        for v in range(len(g.verts)):
            if not g.boundary[v]:
                sides[g.cell_sides(v)] = sides.get(g.cell_sides(v), 0) + 1
        out["cell_sides"] = dict(sorted(sides.items()))
        out["flips"] = getattr(g, "flips", 0)
    return out
