"""岩体与草地的“色彩可能性空间”（x.com/OskSta 2023-10-11 palette authoring tool，按视频逐帧取色）。

视频里的工具：几个颜色节点张成一个色场（四边形 = 两个三角，外加一个指向白色的三角），色场内部三角剖分，
每个小三角取它所在位置的插值色——同一套颜色连续变化、块面分明。这里照搬到山体：
- 岩体色场 = 视频四角色的浅调（灰绿 #5F8F6A、橙 #D5A157、蓝 #4B8598、玫瑰 #AE6874 各混 30% 米白）；
  纵向铺色：每座山从自己的山顶往下取色（橙 → 玫瑰 → 灰绿 → 蓝，山脚与水线最冷），
  横向只有很小的色相偏移；朝天的面与亮层带再沿“白色三角”向米白靠；
  水平层纹明暗交替、悬挑压暗。逐面平涂，不加随机抖动——山不再是灰突突的一种颜色。
- 草地同样纵向：从山顶往下 黄绿 → 橄榄 → 草绿 → 青绿。
- 石料、灌木、柏树只做轻微逐面抖动。
纯 Python；Blender worker 把结果写进逐角颜色属性（导出为 glTF COLOR_0），Godot 导入后按顶点色着色。
"""
from __future__ import annotations

import math

ROCK_ROLES = ("rock", "rock_alt", "rock_dark", "rock_top")
GRASS_ROLES = ("grass",)
JITTER_ROLES = {"stone": 0.035, "stone_alt": 0.03, "shrub": 0.06, "canopy": 0.05, "cypress": 0.05}
TINT_ROLES = ROCK_ROLES + GRASS_ROLES + tuple(JITTER_ROLES)


def srgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def to_linear(c) -> tuple[float, float, float]:
    return tuple(x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c)


# Oskar 调色工具视频（x.com/OskSta 2023-10-11）里的色域：四个角色节点张成一个四边形色场
# （帧采样：左上灰绿 #5F8F6A、右上橙 #D5A157、左下蓝 #4B8598、右下玫瑰 #AE6874），再加一个指向白色的三角。
# 山体取这个色域的“浅调”（与米白混合），每个面在色场里按位置取色：不再是灰色。
VIDEO_QUAD = {"green": srgb("#5F8F6A"), "orange": srgb("#D5A157"), "blue": srgb("#4B8598"), "rose": srgb("#AE6874")}
CREAM = srgb("#F3EBDA")
ROCK_QUAD = {k: _mix(v, CREAM, 0.3) for k, v in VIDEO_QUAD.items()}
GRASS_QUAD = {"yellow": srgb("#B9C25A"), "green": srgb("#7FA650"), "teal": srgb("#4F9070"), "olive": srgb("#8E9A4A")}
# 兼容旧接口：四阶岩体色阶（由色场的四个角派生）
OSK_ROCK = [ROCK_QUAD["blue"], ROCK_QUAD["green"], ROCK_QUAD["rose"], ROCK_QUAD["orange"]]
OSK_GRASS = [GRASS_QUAD["teal"], GRASS_QUAD["green"], GRASS_QUAD["olive"], GRASS_QUAD["yellow"]]


ROCK_RAMP = [ROCK_QUAD["orange"], ROCK_QUAD["rose"], ROCK_QUAD["green"], ROCK_QUAD["blue"]]   # 山顶 → 山脚
GRASS_RAMP = [GRASS_QUAD["yellow"], GRASS_QUAD["olive"], GRASS_QUAD["green"], GRASS_QUAD["teal"]]  # 山顶 → 山脚


def _ramp(cols: list, v: float):
    v = max(0.0, min(1.0, v)) * (len(cols) - 1)
    i = min(int(v), len(cols) - 2)
    return _mix(cols[i], cols[i + 1], v - i)


def _bilerp(q: dict, keys: tuple, u: float, v: float):
    a, b, c, d = (q[k] for k in keys)          # (u=0,v=0) (u=1,v=0) (u=0,v=1) (u=1,v=1)
    top = _mix(a, b, u)
    bot = _mix(c, d, u)
    return _mix(top, bot, v)


class Field:
    """平滑二维场（几组随机方向的正弦叠加，[-1, 1]）：大片的冷暖 / 干湿分区，与格网无关。"""

    def __init__(self, seed: int, wavelength: float, n: int = 5):
        import random
        r = random.Random(seed * 977 + int(wavelength * 10))
        self.w = []
        for _ in range(n):
            a = r.uniform(0, math.pi)
            f = 2 * math.pi / (wavelength * r.uniform(0.75, 1.3))
            self.w.append((math.cos(a) * f, math.sin(a) * f, r.uniform(0, 2 * math.pi)))

    def __call__(self, x: float, y: float) -> float:
        return sum(math.sin(ax * x + ay * y + p) for ax, ay, p in self.w) / math.sqrt(len(self.w) * 0.5) / 1.6


def _hash(x: float, y: float, z: float) -> float:
    v = math.sin(x * 12.9898 + y * 78.233 + z * 37.719) * 43758.5453
    return v - math.floor(v)          # [0, 1)


class Tinter:
    def __init__(self, pal: dict, *, seed: int = 0, top: float = 20.0, level_h: float = 2.0, strength: float = 1.0,
                 peaks=None, mode: str = "oskar"):
        self.pal = pal
        # mode：oskar = 作者视频的四色竖向色场（没有目标图时的风格默认）；
        #       target-mono / target-multi = 配色环节从目标图测得的颜色（单色崖壁只做明暗；多色在测得的几种颜色之间变化）
        self.mode = mode
        self.top = max(4.0, top)
        self.level_h = level_h
        self.k = max(0.0, min(2.0, strength))
        g = lambda k, d: srgb(pal.get(k, d))   # noqa: E731
        rock, grass = g("rock", "#A2A6A5"), g("grass", "#6F8C48")
        self.ref_rock = rock
        self.pal_rock = rock
        self.peaks = peaks          # (x, y) → 这一处山体的局部山顶高度（米）；None 时用整座岛的最高点
        self.pal_grass = grass
        self.big = Field(seed, 16.0)
        self.mid = Field(seed + 1, 6.0)
        self.gf = Field(seed + 2, 11.0)
        self.gf2 = Field(seed + 4, 4.5)
        self.hue = Field(seed + 5, 26.0)
        self.streak = Field(seed + 3, 3.2)

    def face(self, role: str, pts, normal=None) -> tuple[float, float, float]:
        """返回这个面的 sRGB 颜色（0..1）。pts 为面顶点（米，海面 z=0）。"""
        base = srgb(self.pal.get(role, "#888888"))
        n = len(pts)
        cx = sum(p[0] for p in pts) / n
        cy = sum(p[1] for p in pts) / n
        cz = sum(p[2] for p in pts) / n
        if normal is None:
            nx = ny = nz = 0.0
            for i in range(n):
                x0, y0, z0 = pts[i]
                x1, y1, z1 = pts[(i + 1) % n]
                nx += (y0 - y1) * (z0 + z1)
                ny += (z0 - z1) * (x0 + x1)
                nz += (x0 - x1) * (y0 + y1)
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            normal = (nx / ln, ny / ln, nz / ln)
        jit = _hash(round(cx, 2), round(cy, 2), round(cz, 2)) - 0.5
        k = self.k
        if self.mode.startswith("target") and role in ROCK_ROLES + GRASS_ROLES:
            return self._target_face(role, cx, cy, cz, normal)
        if role in ROCK_ROLES:
            # 纵向铺色（视频里色场从山顶往下铺）：每座山从自己的山顶往下取色——
            #   山顶 橙（暖、日晒）→ 玫瑰 → 灰绿 → 蓝（山脚、水线，阴湿）；
            #   v = 1 − 高度 / 局部山顶高度，所以每根岩柱、每座山各自从顶到脚走完整条色带；
            #   横向只有很小的色相偏移（u），不再左右分区。
            top = self.peaks(cx, cy) if self.peaks else self.top
            top = max(top, cz, 2.0)
            v = max(0.0, min(1.0, 1.0 - cz / top))
            if cz < 1.2:
                v = max(v, 0.92)
            up = normal[2]
            c = _ramp(ROCK_RAMP, v)
            u = 0.18 * self.hue(cx, cy)                                          # 轻微横向色相偏移
            c = _mix(c, _ramp(ROCK_RAMP, max(0.0, min(1.0, v + 0.25))), max(0.0, u)) if u > 0 else \
                _mix(c, _ramp(ROCK_RAMP, max(0.0, min(1.0, v - 0.25))), -u)
            band = int(math.floor(cz / self.level_h))
            white = (0.3 if up > 0.6 else 0.0) + (0.15 if abs(up) < 0.5 and (band + seed_band(self)) % 3 == 1 else 0.0)
            c = _mix(c, CREAM, white * k)
            lum = 1.0
            if abs(up) < 0.5:
                lum *= (1.0, 1.05, 0.93)[(band + seed_band(self)) % 3]        # 水平层纹：明暗交替
            if up < -0.3:
                lum *= 0.8                                                       # 悬挑下方
            lum *= {"rock_dark": 0.78, "rock_alt": 1.04, "rock_top": 1.0}.get(role, 1.0)
            c = _mix(srgb("#9A9A96"), c, k) if k < 1 else c                      # strength < 1 时向中性灰收
            c = _mix(c, self.pal_rock, 0.08)                                     # 场景调色盘只轻微偏移
            return tuple(max(0.0, min(1.0, x * (1 + (lum - 1) * k))) for x in c)
        if role in GRASS_ROLES:
            # 草地同样纵向：台面越接近这座山的山顶越偏黄绿 / 金黄，越低越偏青绿；横向只做小偏移
            top = max(self.peaks(cx, cy) if self.peaks else self.top, cz, 2.0)
            v = max(0.0, min(1.0, 1.0 - cz / top + 0.12 * self.gf(cx, cy)))
            return _mix(_ramp(GRASS_RAMP, v), self.pal_grass, 0.15)
        amp = JITTER_ROLES.get(role, 0.0) * k
        return tuple(max(0.0, min(1.0, x * (1 + 2 * amp * jit))) for x in base)


def _target_face(self, role, cx, cy, cz, normal):
    """按目标图测得的颜色上色：本色 = 测得主色；朝上 / 受光的面向测得受光色靠，悬挑与层纹向背光色靠。
    单色崖壁（target-mono）不做色相变化；多色（target-multi）只在测得的主色与 rock_alt 之间小幅过渡。"""
    up = normal[2]
    if role in GRASS_ROLES:
        base, lit, shade = (srgb(self.pal.get(k, d)) for k, d in
                            (("grass", "#7FA650"), ("grass_alt", "#B9C25A"), ("grass_dark", "#4F9070")))
        g = self.gf(cx, cy)
        c = _mix(base, lit, max(0.0, g) * 0.35) if g > 0 else _mix(base, shade, -g * 0.25)
        return c
    base = srgb(self.pal.get("rock", "#A2A6A5"))
    lit = srgb(self.pal.get("rock_alt", self.pal.get("rock", "#A2A6A5")))
    shade = srgb(self.pal.get("rock_dark", "#666B6C"))
    k = self.k
    c = base
    if self.mode == "target-multi":
        c = _mix(base, lit, 0.5 + 0.5 * self.hue(cx, cy))
    if up > 0.6:
        c = _mix(c, lit, 0.45 * k)                                   # 朝天的面：受光
    band = int(math.floor(cz / self.level_h))
    if abs(up) < 0.5:
        c = _mix(c, (lit, base, shade)[(band + seed_band(self)) % 3], 0.18 * k)   # 层纹：在测得的明暗之间交替
    if up < -0.3:
        c = _mix(c, shade, 0.5 * k)                                  # 悬挑下方
    if role == "rock_dark":
        c = _mix(c, shade, 0.4)
    return tuple(max(0.0, min(1.0, x)) for x in c)


Tinter._target_face = _target_face


def seed_band(t: "Tinter") -> int:
    return int(t.big.w[0][2] * 100) % 3


def peak_field(parts: dict, cell: float = 2.0, radius: float = 7.0):
    """局部山顶高度：把岩体顶点的高度落到 2 米格上取最大值，再在 radius 米范围内取最大 → (x, y) → 米。"""
    grid: dict[tuple[int, int], float] = {}
    for r in ROCK_ROLES:
        for x, y, z in (parts.get(r) or {}).get("v", []):
            k = (int(math.floor(x / cell)), int(math.floor(y / cell)))
            if z > grid.get(k, -1e9):
                grid[k] = z
    n = int(math.ceil(radius / cell))
    cache: dict[tuple[int, int], float] = {}

    def at(x: float, y: float) -> float:
        k = (int(math.floor(x / cell)), int(math.floor(y / cell)))
        if k not in cache:
            best = -1e9
            for i in range(-n, n + 1):
                for j in range(-n, n + 1):
                    if i * i + j * j <= n * n:
                        best = max(best, grid.get((k[0] + i, k[1] + j), -1e9))
            cache[k] = best if best > -1e9 else 2.0
        return cache[k]
    return at


def part_colors(parts: dict, pal: dict, *, seed: int = 0, level_h: float = 2.0, strength: float = 1.0,
                mode: str = "oskar") -> dict[str, list]:
    """{角色: [每个面的 sRGB]}（只算需要变化的角色）。"""
    tops = [v[2] for r in ROCK_ROLES if r in parts for v in parts[r]["v"]]
    t = Tinter(pal, seed=seed, top=max(tops) if tops else 20.0, level_h=level_h, strength=strength,
               peaks=peak_field(parts) if tops else None, mode=mode)
    out = {}
    for role in TINT_ROLES:
        if role not in parts:
            continue
        vs = parts[role]["v"]
        out[role] = [t.face(role, [vs[i] for i in f]) for f in parts[role]["f"]]
    return out
