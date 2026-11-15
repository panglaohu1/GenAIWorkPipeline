"""瓦片 WFC 地形（纯 Python，确定性）：在六边形对偶格上坍缩「地表类型 × 高度层」瓦片。

依据 Oskar 星球项目（x.com/OskSta，2024-03 ～ 2024-06）：
- "There's no noise function, no. It's all tile based"；"just random picking of tiles with some rules.
  The tiles themselves enforce some continuity"——没有噪声，只有带规则的随机挑瓦片；
- 河以外的地形按广度优先坍缩，"making nice gentle slopes that only occasionally meet steep cliffs"；
- "an erosion constraint, disallowing local minima"——不允许局部最低点（坑）；
- 大模块先要地："I first generate the specific bit of grid they need"——台地（plateau）先固定，再让周围生长。

瓦片 = (类型, 层)。类型：sea（海，不占格）、grass（草坡）、forest（林地）、rock（裸岩崖顶）。
相邻规则：陆地相邻高差 ≤ 1；较高一侧是 rock 时允许 ≤ cliff_max 的陡崖；海边只能接最低一级陆地或海崖（rock）。
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass

TYPES = ("sea", "grass", "forest", "rock")


@dataclass(frozen=True)
class Tile:
    kind: str
    level: int   # 陆地：顶面层数（格子填 0..level-1）；海：sea_level


def make_tiles(sea_level: int, top: int) -> list[Tile]:
    tiles = [Tile("sea", sea_level)]
    for lv in range(sea_level + 1, top + 1):
        tiles.append(Tile("grass", lv))
        if lv < top:
            tiles.append(Tile("forest", lv))
        if lv >= sea_level + 2:
            tiles.append(Tile("rock", lv))
    return tiles


def compatible(a: Tile, b: Tile, sea_level: int, cliff_max: int) -> bool:
    if a.kind == "sea" and b.kind == "sea":
        return True
    if a.kind == "sea" or b.kind == "sea":
        land = b if a.kind == "sea" else a
        return land.level <= sea_level + 1 or (land.kind == "rock" and land.level <= sea_level + cliff_max)
    d = abs(a.level - b.level)
    if d <= 1:
        return True
    hi = a if a.level > b.level else b
    return hi.kind == "rock" and d <= cliff_max


class TerrainWFC:
    """cells：参与坍缩的格子（顶点编号）；neighbors：对偶格相邻表；outside：强制为海的格子。"""

    def __init__(self, cells: list[int], neighbors: list[list[int]], *, sea_level: int, top: int,
                 cliff_max: int = 3, weights: dict[str, float] | None = None):
        self.cells = list(cells)
        self.cell_set = set(cells)
        self.nb = {v: [w for w in neighbors[v] if w in self.cell_set] for v in cells}
        self.sea_level, self.top, self.cliff_max = sea_level, top, cliff_max
        self.tiles = make_tiles(sea_level, top)
        self.w = {"sea": 0.03, "grass": 1.0, "forest": 0.2, "rock": 0.04, **(weights or {})}
        self.cluster = 2.2
        n = len(self.tiles)
        self.full = (1 << n) - 1
        self.compat = [sum(1 << j for j in range(n) if compatible(self.tiles[i], self.tiles[j], sea_level, cliff_max))
                       for i in range(n)]
        self.index = {t: i for i, t in enumerate(self.tiles)}
        self.stats = {"restarts": 0, "contradictions": 0, "pits_filled": 0, "fallback": 0}

    # ── 定义域 ──
    def mask(self, pred) -> int:
        return sum(1 << i for i, t in enumerate(self.tiles) if pred(t))

    def _support(self, dom: int) -> int:
        s = 0
        i = 0
        while dom:
            if dom & 1:
                s |= self.compat[i]
            dom >>= 1
            i += 1
        return s

    def _propagate(self, dom: dict[int, int], start: list[int]) -> bool:
        q = deque(start)
        queued = set(start)
        while q:
            v = q.popleft()
            queued.discard(v)
            sup = self._support(dom[v])
            for w in self.nb[v]:
                nd = dom[w] & sup
                if nd != dom[w]:
                    if not nd:
                        return False
                    dom[w] = nd
                    if w not in queued:
                        q.append(w)
                        queued.add(w)
        return True

    def _order(self, seeds: list[int], rng: random.Random) -> list[int]:
        """广度优先次序：从锚点（山峰 / 台地 / 中心）向外一圈圈坍缩，同一圈内随机。"""
        dist = {v: 0 for v in seeds}
        q = deque(seeds)
        while q:
            v = q.popleft()
            for w in self.nb[v]:
                if w not in dist:
                    dist[w] = dist[v] + 1
                    q.append(w)
        rest = [v for v in self.cells if v not in dist]
        far = max(dist.values(), default=0) + 1
        for v in rest:
            dist[v] = far
        jitter = {v: rng.random() for v in self.cells}
        return sorted(self.cells, key=lambda v: (dist[v], jitter[v]))

    def _pick(self, v: int, dom: int, levels: dict[int, int], rng: random.Random, descend: float,
              kinds: dict[int, str] | None = None) -> int:
        ref = [levels[w] for w in self.nb[v] if w in levels]
        base = max(ref) if ref else None
        near = [kinds[w] for w in self.nb[v] if kinds and w in kinds]
        opts, ws = [], []
        for i, t in enumerate(self.tiles):
            if not dom >> i & 1:
                continue
            w = self.w[t.kind]
            if t.kind in ("forest", "rock") and near:   # 成片：林地、裸岩跟着已定的同类邻格长（森林是一块，不是满地点）
                w *= 1.0 + self.cluster * near.count(t.kind)
            if base is not None and t.kind != "sea":
                d = t.level - base
                w *= 1.0 if d == 0 else descend if d == -1 else 0.15 if d == 1 else 0.3
            opts.append(i)
            ws.append(max(w, 1e-6))
        return rng.choices(opts, ws)[0]

    def solve(self, fixed: dict[int, int], domains: dict[int, int], seed: int = 0, max_restarts: int = 8,
              descend: float = 0.55) -> dict[int, Tile]:
        """fixed：格子 → 瓦片编号（预留的大模块 / 山峰）；domains：格子 → 允许的瓦片位掩码。"""
        for attempt in range(max_restarts + 1):
            rng = random.Random(seed * 1009 + attempt)
            dom = {v: domains.get(v, self.full) for v in self.cells}
            for v, i in fixed.items():
                if v in dom:
                    dom[v] = 1 << i
            if not self._propagate(dom, list(self.cells)):
                self.stats["contradictions"] += 1
                self.stats["restarts"] += 1
                continue
            levels = {v: self.tiles[i].level for v, i in fixed.items() if v in dom}
            kinds = {v: self.tiles[i].kind for v, i in fixed.items() if v in dom}
            seeds = [v for v in fixed if v in dom] or [self.cells[0]]
            ok = True
            for v in self._order(seeds, rng):
                d = dom[v]
                if d & (d - 1):   # 还没确定
                    i = self._pick(v, d, levels, rng, descend, kinds)
                    dom[v] = 1 << i
                    if not self._propagate(dom, [v]):
                        ok = False
                        self.stats["contradictions"] += 1
                        break
                levels[v] = self.tiles[dom[v].bit_length() - 1].level
                kinds[v] = self.tiles[dom[v].bit_length() - 1].kind
            if ok:
                return self._erode({v: self.tiles[d.bit_length() - 1] for v, d in dom.items()}, fixed)
            self.stats["restarts"] += 1
        # 兜底：不传播，逐格挑与已定邻居相容的瓦片（极少触发；记入 stats）
        self.stats["fallback"] = 1
        rng = random.Random(seed)
        out: dict[int, Tile] = {v: self.tiles[i] for v, i in fixed.items() if v in self.cell_set}
        for v in self._order(list(out) or [self.cells[0]], rng):
            if v in out:
                continue
            allowed = domains.get(v, self.full)
            for w in self.nb[v]:
                if w in out:
                    allowed &= self.compat[self.index[out[w]]]
            if not allowed:
                allowed = 1   # 海
            levels = {w: t.level for w, t in out.items()}
            out[v] = self.tiles[self._pick(v, allowed, levels, rng, descend)]
        return self._erode(out, fixed)

    def _erode(self, res: dict[int, Tile], fixed: dict[int, int]) -> dict[int, Tile]:
        """侵蚀约束：陆地上不留坑（四周全比自己高的草 / 林格抬到最低邻格的高度），水总能流向海。"""
        changed = True
        while changed:
            changed = False
            for v, t in res.items():
                if t.kind in ("sea", "rock") or v in fixed:
                    continue
                around = [res[w].level for w in self.nb[v] if w in res]
                if around and len(around) == len(self.nb[v]) and min(around) > t.level:
                    res[v] = Tile(t.kind if t.kind != "forest" or min(around) < self.top else "grass", min(around))
                    self.stats["pits_filled"] += 1
                    changed = True
        return res


def hops_to(cells: list[int], neighbors: list[list[int]], targets: set[int]) -> dict[int, int]:
    """每个格子到 targets（如岛外海格）的图距离。"""
    dist = {v: 0 for v in targets}
    q = deque(targets)
    cs = set(cells) | set(targets)
    while q:
        v = q.popleft()
        for w in neighbors[v]:
            if w in cs and w not in dist:
                dist[w] = dist[v] + 1
                q.append(w)
    return dist


def wobble_radius(theta: float, seed: float, amount: float) -> float:
    r = random.Random(seed)
    ph = [r.uniform(0, 2 * math.pi) for _ in range(3)]
    return 1.0 + amount * (0.55 * math.sin(2 * theta + ph[0]) + 0.3 * math.sin(3 * theta + ph[1])
                           + 0.15 * math.sin(5 * theta + ph[2]))
