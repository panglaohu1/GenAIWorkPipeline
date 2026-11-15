"""体素层：Oskar 引擎的角点状态 ↔ REACode 的体素 World，跑 REACodeRefactoring/src/capabilities 的能力
（石拱 / 天桥、崖顶悬垂、海中石柱、穿山隧道、海边灯塔…）。

对应关系（两边都是 Townscaper 式「格子 × 层」）：
  Oskar 网格顶点 v（对偶格）      ↔ World.grid.cells[v]，edgeNeighbors = 按方位角排好的相邻顶点；外边界补一条 -1（开阔海面）
  Oskar 第 h 层（0 起）            ↔ World 第 h+1 层（位掩码第 h 位）
  Cell.kind == "rock"             ↔ rock 位；其它（tower / wall / plinth…）是「建筑」（solid 有、rock 无）
  color                           ↔ 该列顶层的组号（建筑），岩石 / 空 为 -1
执行：扫描（match）→ Router（改造方向关键词 + 已启用）→ 在副本上执行 → verify.js 五项检查 → 不是 fail 才写回 Oskar 状态。
在 Blender 工作进程里同步调用（那里没有事件循环）。
"""
from __future__ import annotations

import math

from . import bridge
from .contract import params_for, route

MAX_LV = 30   # int32 位掩码


def state_world(st) -> dict:
    g = st.grid
    n = len(g.verts)
    cols: dict[int, list[tuple[int, object]]] = {}
    for (v, h), c in st.cells.items():
        cols.setdefault(v, []).append((h, c))
    cells, solid, rock, color = [], [0] * n, [0] * n, [-1] * n
    for v in range(n):
        x, y = g.verts[v][0], g.verts[v][1]
        nb = sorted(g.neighbors[v], key=lambda w: math.atan2(g.verts[w][1] - y, g.verts[w][0] - x))
        if g.boundary[v]:
            nb = nb + [-1]
        cells.append({"id": v, "n": len(nb), "edgeNeighbors": nb, "cx": x, "cz": y})
        top_h, top_c = -1, None
        for h, c in cols.get(v, []):
            if h >= MAX_LV:
                continue
            solid[v] |= 1 << h
            if c.kind == "rock":
                rock[v] |= 1 << h
            if h > top_h:
                top_h, top_c = h, c
        if top_c is not None and top_c.kind != "rock":
            color[v] = int(top_c.group)
    return {"cells": cells, "solid": solid, "rock": rock, "color": color, "river": [0] * n}


def apply_world(st, before: dict, after: dict) -> dict:
    """把执行后的位掩码差写回 Oskar 状态：新增的岩石 / 建筑格沿用同列（或邻格）的组，删掉的格直接移除。"""
    from ..oskar.program import Cell
    g = st.grid
    added = removed = 0
    for v in range(len(g.verts)):
        b0, b1 = int(before["solid"][v]), int(after["solid"][v])
        r1 = int(after["rock"][v])
        if b0 == b1 and int(before["rock"][v]) == r1:
            continue
        for h in range(MAX_LV):
            m = 1 << h
            if b0 & m and not b1 & m:
                st.cells.pop((v, h), None)
                removed += 1
            elif b1 & m and not b0 & m:
                is_rock = bool(r1 & m)
                st.cells[(v, h)] = Cell("rock" if is_rock else _kind_near(st, v, h, built=True),
                                        _group_near(st, v, h, rock=is_rock, color=int(after["color"][v])))
                added += 1
    return {"added": added, "removed": removed}


def _near_cells(st, v, h):
    yield from ((st.cells.get((v, hh)), hh) for hh in range(h - 1, -1, -1))
    for w in st.grid.neighbors[v]:
        for hh in (h, h - 1, h + 1):
            yield st.cells.get((w, hh)), hh


def _group_near(st, v, h, *, rock: bool, color: int) -> int:
    if not rock and color >= 0:
        return color
    for c, _ in _near_cells(st, v, h):
        if c is not None and (c.kind == "rock") == rock:
            return c.group
    return max([c.group for c in st.cells.values()] or [0])


def _kind_near(st, v, h, built: bool) -> str:
    for c, _ in _near_cells(st, v, h):
        if c is not None and c.kind != "rock":
            return c.kind
    return "tower"


def apply_to_state(st, plan: dict, *, total: float = 60) -> dict:
    """plan = {"goal": 改造方向文字, "selected": [已启用的能力 id], "steps": [可选：直接给的步骤], "params": {id: {…}}}。
    返回报告（写进构建结果，供管线显示与经验记录）：扫描、路由、执行、验证、是否写回。"""
    ok, why = bridge.available()
    if not ok:
        return {"status": "skipped", "why": why}
    world = state_world(st)
    try:
        caps = bridge.call_sync("describe", {}, total=total).get("voxel") or []
        if isinstance(caps, dict):
            raise bridge.BridgeError(caps.get("error", "体素能力库加载失败"))
        scan = bridge.call_sync("voxel.scan", {"world": world}, total=total)
        metas = {c["id"]: c for c in caps}
        steps = plan.get("steps") or [{"id": r["id"], "params": params_for(metas[r["id"]], (plan.get("params") or {}).get(r["id"]))}
                                      for r in route(plan.get("goal", ""), caps, scan, plan.get("selected") or [])]
        executable = {c["id"] for c in caps if c.get("kind2") == "geometry"}
        steps = [s for s in steps if s["id"] in executable]
        if not steps:
            return {"status": "no-route", "scanned": {m["id"]: m["count"] for m in scan},
                    "why": "改造方向没有提到、管线里也没启用可执行的体素能力"}
        run = bridge.call_sync("voxel.run", {"world": world, "steps": steps}, total=total)
        ver = bridge.call_sync("voxel.verify", {"before": world, "after": run["world"],
                                                "opts": {"touchesBuilt": any(s["id"].startswith("build.") for s in steps)}},
                               total=total)
    except bridge.BridgeError as e:
        return {"status": "error", "why": str(e)[:500]}
    rep = {"status": ver["status"], "steps": [s["id"] for s in steps], "report": run["report"],
           "verify": ver, "scanned": {m["id"]: m["count"] for m in scan}}
    if ver["status"] == "fail":
        rep["applied"] = False
        return rep
    rep["applied"] = True
    rep["diff"] = apply_world(st, world, run["world"])
    return rep
