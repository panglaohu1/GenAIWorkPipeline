"""场景程序（LLM 输出的小型 DSL）→ 角点状态。纯 Python，确定性。

坐标约定（写进 Bonsai 提示词）：平面坐标 x, y 为归一化值 [-1, 1]，(0,0) 为场景中心，
-y 朝向镜头（前），+x 向右；尺寸 r / size 同为归一化值；高度一律用「层」（整数）。
LLM 只描述大形（Oskar：玩家掌控大形，细节交给系统），细节由 WFC 与模块完成。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .grid import Grid, make_grid
from .tiles import TerrainWFC, Tile, hops_to, wobble_radius

KINDS = {"rock", "plinth", "wall", "main", "wing", "tower", "house"}
BUILDING_KINDS = {"main", "wing", "tower", "house"}
ROOFS = {"hip", "dome", "cone", "flat", "garden", "terrace", "crenel"}
# 风格：由调色盘决定默认值（程序里也可显式写 style / rock_style）
STYLE_BY_PALETTE = {"citadel": "mediterranean"}


@dataclass
class Cell:
    kind: str
    group: int


@dataclass
class State:
    grid: Grid
    level_h: float
    base_z: float
    sea_level: int | None                  # 海面位于第几层的顶面（None 表示无海）
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)
    group_roof: dict[int, str] = field(default_factory=dict)
    group_kind: dict[int, str] = field(default_factory=dict)
    paths: list[dict] = field(default_factory=list)     # {"verts": [...], "level": L, "clear": n}
    gates: set[tuple[int, int]] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)       # 自动修正记录（反馈给 LLM）
    waterfalls: list[dict] = field(default_factory=list)  # {"at": (x, y) 米, "dir": (dx, dy), "w": 米}
    crystals: list[dict] = field(default_factory=list)    # {"at", "r" 米, "bottom" 层或 None, "top" 层}
    trees: list[dict] = field(default_factory=list)       # {"at", "r" 米（树冠）, "h" 米, "bottom" 层或 None}
    vegetation: str = "shrub"       # shrub：灌木；trees：台地上自动成林（大冠树）；cypress：成排柏树
    terrace_rule: bool = True       # 海蚀岩柱场景：大岩柱须有 ≥2 级宽台面（其他场景不查）
    rails: list[dict] = field(default_factory=list)        # 细轨高架：{"pts": [(x, y)...], "z": 米}
    arch_span: dict[tuple[int, int], float] = field(default_factory=dict)  # 桥拱下的空格 → 半跨（米），拱为半圆
    style: str = "plain"            # mediterranean：拱窗、顶层拱廊、平顶栏杆、成排柏树
    rock_style: str = "columnar"    # columnar：竖向棱脊 + 通高裂隙；layered：逐层外鼓 + 层缝
    batter: float | None = None     # 收分：每低于平滑顶面 1 层外放的米数（None = 按岩体风格默认；0 = 关闭）
    soften: float | None = None     # 平面轮廓软化最大位移（米）；None = 默认 0.9，0 = 关闭
    rock_detail: float = 1.0        # 岩面细节强度 0..1（噪声起伏、棱脊、层缝 / 裂隙）；目标图实测崖面平整时调低
    sea_plate: bool = True          # 是否做海面板（六边形海面）；场景另有海面资产时关掉，只留岸边浪花
    facet: tuple[float, float] | None = None   # 块面起伏 (幅度, 波长)（米）：低多边形崖的大斜块，按目标图实测偏角定
    bare_groups: set[int] = field(default_factory=set)      # 顶面一律裸岩（瓦片 WFC 的 rock 崖顶）
    measured_groups: set[int] = field(default_factory=set)  # 顶面草 / 岩按目标图实测（不再叠风格的随机裸岩斑）
    tiles: dict[int, tuple[str, int]] = field(default_factory=dict)   # 瓦片 WFC 结果：格子 → (类型, 顶层)
    terrain_stats: dict = field(default_factory=dict)

    def z(self, h: int) -> float:
        return self.base_z + h * self.level_h

    @property
    def sea_z(self) -> float | None:
        return None if self.sea_level is None else self.z(self.sea_level)

    def get(self, v: int, h: int) -> Cell | None:
        return self.cells.get((v, h))

    def top(self, v: int) -> int:
        hs = [h for (vv, h) in self.cells if vv == v]
        return max(hs) + 1 if hs else 0


def _pt(p, S):
    return (float(p[0]) * S, float(p[1]) * S)


def _rot(x, y, deg):
    a = math.radians(deg)
    return x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)


def _seg_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _poly_dist(p, pts) -> float:
    return min(_seg_dist(p, pts[i], pts[i + 1]) for i in range(len(pts) - 1)) if len(pts) > 1 else math.dist(p, pts[0])


def _wobble(theta: float, rng: random.Random, amount: float) -> float:
    """岩柱轮廓的低频起伏（按角度），让外形有机但不碎。"""
    k1, k2, p1, p2 = rng.choice((2, 3)), rng.choice((4, 5)), rng.random() * 6.28, rng.random() * 6.28
    return 1 + amount * (0.6 * math.sin(k1 * theta + p1) + 0.4 * math.sin(k2 * theta + p2))


def _palette_name(prog: dict) -> str:
    p = prog.get("palette")
    return p if isinstance(p, str) else str(prog.get("palette_base") or "")


def _fill_below(st: State, members, bottom: int, gid: int, plinth_gid: int) -> tuple[int, int]:
    """向下补地基直到碰到已有格子或地面：贴着上部的 2 层用石砌（plinth），更深的用岩体（像从岩崖长出来）。
    返回 (补的格数, 新的 gid 计数)。"""
    filled = 0
    rock_gid = None
    for v in members:
        h = bottom - 1
        while h >= 0 and (v, h) not in st.cells:
            if h < bottom - 2:
                if rock_gid is None:
                    gid += 1
                    rock_gid = gid
                    st.group_kind[rock_gid] = "rock"
                    st.group_roof[rock_gid] = "cliff"
                st.cells[(v, h)] = Cell("rock", rock_gid)
            else:
                st.cells[(v, h)] = Cell("plinth", plinth_gid)
            filled += 1
            h -= 1
    return filled, gid


def validate_program(prog: dict) -> str | None:
    """给 LLM 的即时反馈：结构性错误用简短中文指出。"""
    if not isinstance(prog, dict) or not isinstance(prog.get("items"), list) or not prog["items"]:
        return "需要 {\"items\": [...]} 且非空"
    for i, it in enumerate(prog["items"]):
        t = it.get("type")
        if t not in {"terrain", "stack", "mass", "building", "wall", "path", "clear", "waterfall", "bridge", "crystal", "tree"}:
            return f"items[{i}].type 必须是 terrain/stack/mass/building/wall/path/bridge/waterfall/crystal/tree/clear 之一"
        if t == "terrain":
            if not isinstance(it.get("top"), (int, float)):
                return f"items[{i}] terrain 需要 top：最高处的层数（整数）"
            if i != 0:
                return f"items[{i}] terrain 必须写在 items 第一个（先有地形，再放建筑、路和桥）"
            for key in ("peaks", "plateaus"):
                for j, a in enumerate(it.get(key) or []):
                    if not (isinstance(a, dict) and isinstance(a.get("at"), list) and len(a["at"]) == 2
                            and isinstance(a.get("top"), (int, float))):
                        return f"items[{i}].{key}[{j}] 需要 at:[x, y] 和 top（整数层）"
            continue
        if t == "crystal" and not isinstance(it.get("top"), (int, float)):
            return f"items[{i}] crystal 需要 top：晶柱顶端所在层（整数）"
        if t not in ("path", "wall", "bridge") and not (isinstance(it.get("at"), list) and len(it["at"]) == 2):
            return f"items[{i}].at 需要 [x, y]（-1..1）"
        if t == "bridge" and not isinstance(it.get("level"), (int, float)):
            return f"items[{i}] bridge 需要 level：桥面所在层（整数）"
        for key in ("at",):
            if key in it and any(abs(float(c)) > 1.2 for c in it[key]):
                return f"items[{i}].at 超出范围：x, y 必须在 -1..1 之间"
        if t in ("wall", "path", "bridge"):
            pts = it.get("path") or it.get("points")
            if not (isinstance(pts, list) and len(pts) >= 2):
                return f"items[{i}] 需要 path/points：至少 2 个 [x, y]"
        if t == "stack" and not it.get("tiers"):
            return f"items[{i}] stack 需要 tiers：[{{\"r\":0.2,\"top\":4}}, ...]"
        if t in ("mass", "building") and not (it.get("size") or it.get("r")):
            return f"items[{i}] 需要 size:[宽,深] 或 r"
        if t == "building" and it.get("kind", "house") not in BUILDING_KINDS:
            return f"items[{i}].kind 必须是 {sorted(BUILDING_KINDS)} 之一"
    return None


def grid_kind(prog: dict) -> str:
    """基础格：默认 hex（三角网 + 六边形对偶格，Oskar 星球项目）；旧程序可写 grid.kind = "quad"（Townscaper 四边形）。"""
    k = str((prog.get("grid") or {}).get("kind") or "hex")
    return k if k in ("hex", "quad") else "hex"


def build_state(prog: dict) -> State:
    g = prog.get("grid") or {}
    grid = make_grid(radius=int(g.get("radius", 7)), seed=int(prog.get("seed", 0)), cell=float(g.get("cell", 2.0)),
                     kind=grid_kind(prog))
    L = grid.cell * 2
    S = grid.radius * L * 0.85  # 归一化坐标 1.0 对应的米数
    sea = prog.get("sea_level")
    st = State(grid=grid, level_h=float(prog.get("level_h", 2.0)),
               base_z=-(float(sea) * float(prog.get("level_h", 2.0))) if sea is not None else 0.0,
               sea_level=int(sea) if sea is not None else None,
               style=prog.get("style") or STYLE_BY_PALETTE.get(_palette_name(prog), "plain"),
               rock_style=prog.get("rock_style", "columnar"),
               batter=float(prog["batter"]) if isinstance(prog.get("batter"), (int, float)) else None,
               soften=float(prog["soften"]) if isinstance(prog.get("soften"), (int, float)) else None)
    if prog.get("sea_plate") is False:
        st.sea_plate = False
    fc = prog.get("facet")
    if isinstance(fc, (list, tuple)) and len(fc) == 2 and all(isinstance(x, (int, float)) for x in fc) and fc[1] > 0:
        st.facet = (max(0.0, float(fc[0])), float(fc[1]))
    if isinstance(prog.get("rock_detail"), (int, float)):
        st.rock_detail = max(0.0, min(1.0, float(prog["rock_detail"])))
    st.vegetation = prog.get("vegetation") or ("cypress" if st.style == "mediterranean" else "shrub")
    stype = prog.get("scene_type")
    st.terrace_rule = stype == "sea_stacks" if stype else (prog.get("palette") == "sea_stack")
    if st.soften is None:   # 平面轮廓软化：独立岩柱力度大；复杂台地较轻（位移场已平滑，不会交叉折面）
        st.soften = 1.2 if st.terrace_rule else 0.8
    rng_master = random.Random(int(prog.get("seed", 0)) * 7919 + 13)
    inner = [v for v in range(len(grid.verts)) if not grid.boundary[v]]
    P = grid.verts
    gid = 0
    for it in prog["items"]:
        t = it.get("type")
        rng = random.Random(rng_master.random())
        if t == "terrain":
            gid = _terrain(st, it, S, rng, gid)
        elif t == "stack":
            gid += 1
            st.group_kind[gid] = "rock"
            st.group_roof[gid] = "terrace"
            cx, cy = _pt(it["at"], S)
            jit = float(it.get("jitter", 0.15))
            best: dict[int, int] = {}
            for tier in it["tiers"]:
                sx, sy = _pt(tier.get("shift", [0, 0]), S)
                if float(tier["r"]) < 0.05:  # 补救：太细的一级只覆盖一两格，像烟囱
                    st.notes.append(f"stack@{it['at']} 的一级 r={tier['r']} 太细，已放大到 0.05")
                r = max(0.05, float(tier["r"])) * S
                top = int(tier["top"])
                wseed = rng.random()
                for v in inner:
                    dx, dy = P[v][0] - cx - sx, P[v][1] - cy - sy
                    th = math.atan2(dy, dx)
                    rr = r * _wobble(th, random.Random(wseed), jit)
                    if math.hypot(dx, dy) <= rr:
                        # 冠部破碎：最高一级的顶层随机缺一两层
                        tt = top
                        if tier is it["tiers"][-1] and it.get("broken_crown", True):
                            tt = top - (random.Random(v * 31 + gid).random() < 0.35)
                        best[v] = max(best.get(v, 0), tt)
            for v, tt in best.items():
                for h in range(int(it.get("bottom", 0)), tt):
                    st.cells[(v, h)] = Cell("rock", gid)
        elif t in ("mass", "building"):
            gid += 1
            kind = it.get("kind", "plinth" if t == "mass" else "house")
            kind = kind if kind in KINDS else ("plinth" if t == "mass" else "house")
            st.group_kind[gid] = kind
            roof = it.get("roof") or ("dome" if kind == "tower" else "hip" if kind in BUILDING_KINDS else
                                      "crenel" if kind == "wall" else "flat")
            st.group_roof[gid] = roof if roof in ROOFS else "hip"
            cx, cy = _pt(it["at"], S)
            if it.get("size"):
                hx, hy = float(it["size"][0]) * S / 2, float(it["size"][1]) * S / 2
            else:
                hx = hy = float(it["r"]) * S
            ell = it.get("shape", "rect" if it.get("size") else "ellipse") == "ellipse"
            bottom, top = int(it.get("bottom", 0)), int(it["top"])
            members = []
            for v in inner:
                x, y = _rot(P[v][0] - cx, P[v][1] - cy, float(it.get("rot", 0)))
                inside = (x / hx) ** 2 + (y / hy) ** 2 <= 1 if ell else abs(x) <= hx and abs(y) <= hy
                if inside:
                    members.append(v)
            cliff_gid = None
            if kind == "plinth" and bottom == 0 and top > 3:
                # 补救：高台基从地面起 → 下部转为岩崖，只保留顶部 2 层石砌台基（Bonsai 常把整座崖写成台基，渲染成石墩）
                gid += 1
                cliff_gid = gid
                st.group_kind[cliff_gid] = "rock"
                st.group_roof[cliff_gid] = "cliff"   # 城堡下的崖体：不参与海蚀岩柱的台面检查
                st.notes.append(f"plinth@{it['at']} 高 {top} 层：下部 {top - 2} 层已转为岩崖，顶部 2 层为石台基")
            for v in members:
                for h in range(bottom, top):
                    if cliff_gid is not None and h < top - 2:
                        st.cells[(v, h)] = Cell("rock", cliff_gid)
                    else:
                        st.cells[(v, h)] = Cell(kind, gid if cliff_gid is None else gid - 1)
            # 补救：建筑 / 台基下方悬空时用石台基填实（Bonsai 常把建筑放在半空、台基比下面的岩顶宽）
            if it.get("ground", True) and bottom > 0 and members:
                filled, gid = _fill_below(st, members, bottom, gid, gid)
                if filled:
                    st.notes.append(f"{kind}@{it['at']} 下方悬空，已自动补石台基 {filled} 格")
            if not members:
                st.notes.append(f"{t}@{it['at']} 太小，没有覆盖任何格子（请加大 size/r）")
            elif kind in ("main", "wing") and roof == "dome":
                # 宫城规则：大体量上的穹顶 = 平顶（带栏杆）+ 中央鼓座（1–2 层，拱窗）+ 穹顶 + 采光亭
                st.group_roof[gid] = "flat"
                gid += 1
                st.group_kind[gid] = "tower"
                st.group_roof[gid] = "dome"
                rr = min(hx, hy) * float(it.get("dome_r", 0.62))
                drum = 2 if min(hx, hy) >= 3 * grid.cell else 1
                dm = [v for v in members if math.hypot(P[v][0] - cx, P[v][1] - cy) <= rr]
                if not dm:
                    dm = [min(members, key=lambda v: math.hypot(P[v][0] - cx, P[v][1] - cy))]
                for v in dm:
                    for h in range(top, top + drum):
                        st.cells[(v, h)] = Cell("tower", gid)
        elif t == "wall":
            gid += 1
            st.group_kind[gid] = "wall"
            st.group_roof[gid] = "crenel"
            wall_groups = {gid}
            pts = [_pt(p, S) for p in it["path"]]
            w = max(float(it.get("width", 0.05)) * S, grid.cell * 0.55)
            bottom, top = int(it.get("bottom", 0)), int(it["top"])
            for v in inner:
                if _poly_dist(P[v], pts) <= w:
                    for h in range(bottom, top):
                        st.cells[(v, h)] = Cell("wall", gid)
            # 城堡规则：城墙转折点与端点长出塔楼（比墙高 tower_extra 层），形成主次轮廓
            if it.get("towers", "corners") != "none":
                tr = max(w * 1.35, grid.cell * 1.0)
                pts_t = pts if it.get("towers", "corners") == "corners" else [pts[0], pts[-1]]
                for tp in pts_t:
                    gid += 1
                    wall_groups.add(gid)
                    st.group_kind[gid] = "tower"
                    st.group_roof[gid] = it.get("tower_roof") or ("crenel" if st.style == "mediterranean" else "cone")
                    for v in inner:
                        if math.hypot(P[v][0] - tp[0], P[v][1] - tp[1]) <= tr:
                            for h in range(bottom, top + int(it.get("tower_extra", 2))):
                                st.cells[(v, h)] = Cell("tower", gid)
            # 补救：城墙（含墙角塔楼）下方悬空时同样补地基（实测 Bonsai 常把城墙画到崖体外）
            if bottom > 0 and it.get("ground", True):
                wall_verts = sorted({v for (v, h), c in st.cells.items() if h == bottom and c.group in wall_groups})
                gid += 1
                st.group_kind[gid] = "plinth"
                st.group_roof[gid] = "flat"
                filled, gid = _fill_below(st, wall_verts, bottom, gid, gid)
                if filled:
                    st.notes.append(f"城墙@{it['path'][0]} 下方悬空，已自动补石基 / 岩体 {filled} 格")
            for gt in it.get("gates") or []:
                gx, gy = _pt(gt["at"], S)
                gh = int(gt.get("height", 2))
                for v in inner:
                    if math.hypot(P[v][0] - gx, P[v][1] - gy) <= w * 1.25:
                        for h in range(bottom, bottom + gh):
                            st.cells.pop((v, h), None)
                            st.gates.add((v, h))
        elif t == "path":
            pts = [_pt(p, S) for p in it["points"]]
            w = max(float(it.get("width", 0.04)) * S, grid.cell * 0.75)
            lv = it.get("levels") or it.get("level", 0)
            l0, l1 = (int(lv[0]), int(lv[-1])) if isinstance(lv, list) else (int(lv), int(lv))
            if not isinstance(lv, list) and pts:
                # 补救：只给一个高度时，起点取起点处的地面（或海面）高度，一路做台阶升到给定高度
                v0 = min(inner, key=lambda v: math.dist(P[v], pts[0]))
                ground = max([h + 1 for (vv, h) in st.cells if vv == v0], default=st.sea_level or 0)
                if ground < l1 - 1:
                    l0 = ground
                    st.notes.append(f"通路只给了一个高度 {l1}：起点按地面取第 {l0} 层，自动做台阶")
            clear = int(it.get("clear", 2))
            # 沿折线的累计长度参数，用于起止层插值（台阶 / 坡道）
            segl = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
            total = sum(segl) or 1.0

            def along(p):
                best, acc, bt = 1e9, 0.0, 0.0
                for i, L in enumerate(segl):
                    d = _seg_dist(p, pts[i], pts[i + 1])
                    if d < best:
                        a, b = pts[i], pts[i + 1]
                        t0 = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (L * L)))
                        best, bt = d, (acc + t0 * L) / total
                    acc += L
                return bt
            verts = [v for v in inner if _poly_dist(P[v], pts) <= w]
            gid += 1
            st.group_kind[gid] = "plinth"
            st.group_roof[gid] = "stair"   # 平顶台阶：不加栏杆、不种树
            filled = 0
            walk = {}
            for v in verts:
                lvl = round(l0 + (l1 - l0) * along(P[v]))
                walk[v] = lvl
                for h in range(lvl, lvl + clear):
                    st.cells.pop((v, h), None)
                for h in range(0, lvl):   # 补救：路下缺地面时自动填石（堤道 / 台阶），不留悬空路段
                    if (v, h) not in st.cells:
                        st.cells[(v, h)] = Cell("plinth", gid)
                        filled += 1
            if filled:
                st.notes.append(f"通路下方缺地面，已自动填石成台阶/堤道 {filled} 格")
            st.paths.append({"verts": verts, "level": min(walk.values(), default=l0), "clear": clear,
                             "points": it["points"], "walk": walk})
        elif t == "bridge" and it.get("kind") == "rail":
            # 细轨高架（莫比斯轨道 / 廊桥）：不占格子，引擎沿折线生成细梁 + 细柱
            st.rails.append({"pts": [_pt(p, S) for p in it["points"]], "z": st.z(int(it["level"]))})
        elif t == "bridge":
            # 拱桥 / 高架桥：一层厚的桥面 + 等距桥墩（落到第 0 层），墩间为半圆拱（Bonsai 以前只能拿城墙凑）
            pts = [_pt(p, S) for p in it["points"]]
            w = max(float(it.get("width", 0.05)) * S, grid.cell * 0.75)
            deck = max(1, int(it["level"]))
            spacing = max(4.0, float(it.get("span", 0.3)) * S)   # 桥墩中心距（米）
            segl = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]

            def along_m(p):
                best, acc, bt = 1e9, 0.0, 0.0
                for i, L in enumerate(segl):
                    d = _seg_dist(p, pts[i], pts[i + 1])
                    if d < best:
                        a, b = pts[i], pts[i + 1]
                        t0 = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (L * L)))
                        best, bt = d, acc + t0 * L
                    acc += L
                return bt
            gid += 1
            st.group_kind[gid] = "plinth"
            st.group_roof[gid] = "deck"    # 桥面：平顶 + 栏杆，不种树
            pier_w = grid.cell * 0.6
            for v in inner:
                if _poly_dist(P[v], pts) > w:
                    continue
                s = along_m(P[v])
                off = (s + spacing / 2) % spacing - spacing / 2      # 到最近桥墩中心的有符号距离
                pier = abs(off) <= pier_w or s <= pier_w or s >= sum(segl) - pier_w   # 两端必为桥墩（桥台）
                lo = 0 if pier else deck - 1
                for h in range(lo, deck):
                    if (v, h) not in st.cells:
                        st.cells[(v, h)] = Cell("plinth", gid)
                if not pier:
                    for h in range(0, deck - 1):
                        if (v, h) not in st.cells:
                            st.gates.add((v, h))
                    st.arch_span[(v, deck - 1)] = spacing / 2 - pier_w * 0.5
        elif t == "crystal":
            st.crystals.append({"at": _pt(it["at"], S), "r": max(0.03, float(it.get("r", 0.06))) * S,
                                "bottom": it.get("bottom"), "top": int(it["top"]), "seed": rng.randrange(10 ** 6)})
        elif t == "tree":
            r_m = max(0.02, float(it.get("r", 0.05))) * S
            h_m = int(it["height"]) * st.level_h if it.get("height") else r_m * 3.2   # height：树高（层）
            st.trees.append({"at": _pt(it["at"], S), "r": r_m, "h": h_m, "bottom": it.get("bottom"),
                             "seed": rng.randrange(10 ** 6)})
        elif t == "waterfall":
            d = it.get("dir") or [0, -1]
            dl = math.hypot(float(d[0]), float(d[1])) or 1.0
            st.waterfalls.append({"at": _pt(it["at"], S), "dir": (float(d[0]) / dl, float(d[1]) / dl),
                                  "w": max(float(it.get("width", 0.05)) * S, grid.cell * 0.6)})
        elif t == "clear":
            cx, cy = _pt(it["at"], S)
            r = float(it.get("r", 0.1)) * S
            for v in inner:
                if math.hypot(P[v][0] - cx, P[v][1] - cy) <= r:
                    for h in range(int(it.get("bottom", 0)), int(it.get("top", 99))):
                        st.cells.pop((v, h), None)
    return st


def _terrain(st: State, it: dict, S: float, rng: random.Random, gid: int) -> int:
    """瓦片 WFC 地形（Oskar 星球项目做法）：岛形范围内坍缩「草坡 / 林地 / 裸岩崖 / 海 × 高度层」瓦片。
    peaks：山峰锚点（广度优先从这里向外坍缩，越往外越低）；plateaus：预留给大模块的平台（先固定，再让周围生长）。"""
    g, P = st.grid, st.grid.verts
    sea = st.sea_level if st.sea_level is not None else 0
    top = max(sea + 1, int(it["top"]))
    cliff_max = max(2, int(it.get("cliff_max", 3)))
    cx, cy = _pt(it.get("at", [0, 0]), S)
    R = float(it.get("r", 0.85)) * S
    amount = float(it.get("wobble", 0.22))
    wseed = rng.random()
    allv = list(range(len(P)))
    rel = it.get("relief") if isinstance(it.get("relief"), dict) else None
    meas_lv: dict[int, int] = {}
    meas_bare: set[int] = set()   # 实测顶面是裸岩的格子（其余实测格子顶面为草 / 土）
    if rel and rel.get("h"):
        # 实测地形（深度定位的俯视高度图）：轮廓与每格高度都按目标图，引擎只负责把它做成瓦片
        hg, n = rel["h"], len(rel["h"])
        L = max(1, top - sea)
        # 连续的面不切台阶：相邻格实测高差小于半层的连成一块，整块按中位高度定层——台阶只出现在实测有高差（崖坎）的地方
        comp: dict[tuple[int, int], int] = {}
        lvl_of: dict[int, int] = {}
        for i0 in range(n):
            for j0 in range(n):
                if hg[i0][j0] is None or (i0, j0) in comp:
                    continue
                cid = len(lvl_of)
                comp[(i0, j0)] = cid
                stack_, hs = [(i0, j0)], []
                while stack_:
                    a, b = stack_.pop()
                    hs.append(float(hg[a][b]))
                    for aa, bb in ((a - 1, b), (a + 1, b), (a, b - 1), (a, b + 1)):
                        if (0 <= aa < n and 0 <= bb < n and hg[aa][bb] is not None and (aa, bb) not in comp
                                and abs(float(hg[aa][bb]) - float(hg[a][b])) < 0.5 / L):
                            comp[(aa, bb)] = cid
                            stack_.append((aa, bb))
                hs.sort()
                lvl_of[cid] = max(1, min(L, int(round(hs[len(hs) // 2] * L))))
        bare_g = rel.get("bare")
        for v in allv:
            if g.boundary[v]:
                continue
            j = int(((P[v][0] - cx) / R + 1) / 2 * n)
            i = int(((P[v][1] - cy) / R + 1) / 2 * n)
            if 0 <= i < n and 0 <= j < n and hg[i][j] is not None:
                meas_lv[v] = sea + lvl_of[comp[(i, j)]]
                if bare_g and bare_g[i][j]:
                    meas_bare.add(v)
        if len(meas_lv) < 6:
            st.notes.append("实测高度图覆盖的格子太少，退回按岛形生成")
            meas_lv = {}
    if meas_lv:
        land = set(meas_lv)
        cliff_max = max(cliff_max, top - sea)   # 崖可以从水面直上顶面（目标图的整面崖）
    else:
        land = {v for v in allv if not g.boundary[v] and
                math.hypot(P[v][0] - cx, P[v][1] - cy) <= R * wobble_radius(math.atan2(P[v][1] - cy, P[v][0] - cx), wseed, amount)}
    outside = set(allv) - land
    W = TerrainWFC(allv, g.neighbors, sea_level=sea, top=top, cliff_max=cliff_max,
                   weights={"forest": float(it.get("forest", 0.2)), "rock": float(it.get("cliffs", 0.04)),
                            "sea": float(it.get("bays", 0.03))})
    hops = hops_to(sorted(land), g.neighbors, outside)
    domains = {}
    for v in allv:
        if v in outside:
            domains[v] = W.mask(lambda tl: tl.kind == "sea")
        elif meas_lv:
            domains[v] = W.mask(lambda tl, L=meas_lv[v]: tl.kind != "sea" and tl.level == L)
        else:
            hp = hops.get(v, 99)   # 离岸 hp 格：坡度至多每格 1 层（崖可以多 cliff_max-1 层）
            domains[v] = W.mask(lambda tl, hp=hp: (tl.kind == "sea" and hp <= 2)   # 海湾只在近岸，岛中不留海洞
                                or (tl.kind != "sea" and tl.level <= sea + hp)
                                or (tl.kind == "rock" and tl.level <= sea + hp + cliff_max - 1))
    fixed: dict[int, int] = {}
    reserved = 0
    for pl in it.get("plateaus") or []:   # 大模块先要地：台地整片固定为同一层草地
        px, py = _pt(pl["at"], S)
        pr = max(float(pl.get("r", 0.12)) * S, g.cell * 1.2)
        lv = max(sea + 1, min(top, int(pl["top"])))
        for v in land:
            if math.hypot(P[v][0] - px, P[v][1] - py) <= pr:
                if meas_lv:   # 实测地形：台地取该处实测高度；挨着海的格子留给崖（不能是草）
                    if any(w in outside for w in g.neighbors[v]):
                        continue
                    lv = meas_lv[v]
                fixed[v] = W.index[Tile("grass", lv)]
                reserved += 1
    peaks = [] if meas_lv else (it.get("peaks") or [{"at": it.get("at", [0, 0]), "top": top}])
    for pk in peaks:
        px, py = _pt(pk["at"], S)
        lv = max(sea + 1, min(top, int(pk["top"])))
        cand = [v for v in land if v not in fixed]
        if not cand:
            continue
        v0 = min(cand, key=lambda v: math.hypot(P[v][0] - px, P[v][1] - py))
        kind = "rock" if lv >= sea + 3 and it.get("rocky_peaks", True) else "grass"
        fixed[v0] = W.index[Tile(kind, lv)]
        pr = float(pk.get("r", 0)) * S
        for v in land:   # 山体：峰顶附近至少 top-1 层（山脊成片，而不是一根针）
            if v != v0 and v not in fixed and pr and math.hypot(P[v][0] - px, P[v][1] - py) <= pr:
                domains[v] &= W.mask(lambda tl, lv=lv: tl.kind != "sea" and tl.level >= lv - 1)
    res = W.solve(fixed, domains, seed=int(it.get("seed", rng.randrange(10 ** 6))))
    gid += 1
    soil = gid
    st.group_kind[soil] = "rock"
    st.group_roof[soil] = "terrace"
    gid += 1
    bare = gid
    st.group_kind[bare] = "rock"
    st.group_roof[bare] = "terrace"
    st.bare_groups.add(bare)
    if meas_lv:
        st.measured_groups.update((soil, bare))
    counts: dict[str, int] = {}
    for v, tl in res.items():
        counts[tl.kind] = counts.get(tl.kind, 0) + 1
        if tl.kind == "sea":
            continue
        if meas_lv and v in meas_lv:
            # 实测地形：瓦片种类只用来保证崖的相容规则；顶面是草还是裸岩按目标图（崖顶一直是草的岛不留一圈裸岩）
            st.tiles[v] = ("rock" if v in meas_bare else "grass", tl.level)
            grp = bare if v in meas_bare else soil
        else:
            st.tiles[v] = (tl.kind, tl.level)
            grp = bare if tl.kind == "rock" else soil
        for h in range(0, tl.level):
            st.cells[(v, h)] = Cell("rock", grp)
        if tl.kind == "forest":   # 林地：每格一棵大冠树，冠幅略大于格子，连成一片林冠
            trng = random.Random(v * 7919 + int(it.get("seed", 0)))
            r_m = g.cell * trng.uniform(0.85, 1.2)
            st.trees.append({"at": (P[v][0] + trng.uniform(-0.25, 0.25) * g.cell, P[v][1] + trng.uniform(-0.25, 0.25) * g.cell),
                             "r": r_m, "h": r_m * trng.uniform(2.0, 2.7), "bottom": tl.level, "seed": trng.randrange(10 ** 6)})
    st.terrain_stats = {"tiles": counts, "reserved": reserved, "peaks": len(peaks), "measured_cells": len(meas_lv), **W.stats}
    if meas_lv:
        st.notes.append(f"地形按目标图实测高度图生成：{len(meas_lv)} 格")
    st.notes.append(f"瓦片 WFC 地形：{counts}，预留台地 {reserved} 格，重启 {W.stats['restarts']} 次，填坑 {W.stats['pits_filled']} 格")
    return gid


# ───────────────────────── 硬检查（给 LLM 的具体反馈） ─────────────────────────

def check_state(st: State) -> list[dict]:
    g = st.grid
    out = []
    # 1. 连通到地面：从第 0 层出发，经同顶点上下、相邻顶点同层扩散
    seen = {k for k in st.cells if k[1] == 0}
    stack = list(seen)
    while stack:
        v, h = stack.pop()
        for nb in [(v, h - 1), (v, h + 1)] + [(w, h) for w in g.neighbors[v]]:
            if nb in st.cells and nb not in seen:
                seen.add(nb)
                stack.append(nb)
    floating = [k for k in st.cells if k not in seen]
    groups = sorted({st.cells[k].group for k in floating})
    out.append({"name": "无悬空（全部连到地面）", "required": True, "passed": not floating,
                "detail": f"{len(floating)} 格悬空，组 {groups}" if floating else ""})
    # 2. 岩柱台面：每根岩柱至少两级可见台面，主台面宽度 ≥ 局部直径 20%
    for gid, kind in st.group_kind.items():
        if not st.terrace_rule or kind != "rock" or st.group_roof.get(gid) == "cliff":
            continue
        cols = {}
        for (v, h), c in st.cells.items():
            if c.group == gid:
                cols[v] = max(cols.get(v, 0), h + 1)
        if not cols:
            continue
        levels = sorted(set(cols.values()))
        tops = {lv: sum(1 for v in cols if cols[v] == lv) for lv in levels}
        foot = len(cols)
        if foot < 10:  # 远处小岩柱本就是单根石柱，不要求多级台面
            continue
        wide = [lv for lv, n in tops.items() if n >= max(2, 0.2 * foot)]
        out.append({"name": f"岩柱{gid} 台面层次（≥2 级宽台面）", "required": True, "passed": len(wide) >= 2,
                    "detail": f"顶面分布 {tops}，宽台面层 {wide}，占地 {foot} 格"})
    # 3. 通路净空与连续
    for i, p in enumerate(st.paths):
        walk = p.get("walk") or {v: p["level"] for v in p["verts"]}
        blocked = [v for v in p["verts"] for h in range(walk[v], walk[v] + p["clear"]) if (v, h) in st.cells]
        floor_missing = [v for v in p["verts"] if walk[v] > 0 and (v, walk[v] - 1) not in st.cells]
        vs = set(p["verts"])
        comp = 0
        todo = set(vs)
        while todo:
            comp += 1
            s = [todo.pop()]
            while s:
                x = s.pop()
                for w in g.neighbors[x]:
                    if w in todo:
                        todo.remove(w)
                        s.append(w)
        ok = not blocked and comp <= 1 and len(floor_missing) <= max(1, len(vs) // 6)
        out.append({"name": f"通路{i} 净空与连续", "required": True, "passed": ok,
                    "detail": f"{len(vs)} 格，阻挡 {len(blocked)}，断开成 {comp} 段，缺地面 {len(floor_missing)}"})
    return out
