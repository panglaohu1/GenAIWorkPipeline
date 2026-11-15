"""物件层：场景组装的布局 ↔ 系统工坊的「物件布局」，跑 system-workshop/object 的能力
（同类尺寸变化、朝向变化、间距松弛）和它的 verify.js（数值 / 范围 / 手工改动保留 / 位移预算 / 重叠 / 出界 / 隐藏）。

对应关系：
  布局实例 {id, asset, at:[x,y,z], rot_z（度）, scale}  ↔  layout 行 {id, kind=asset, name, x, z, r, edit:{dx,dy,dz,ry,s,hidden}, locked}
  我们是 Z 朝上：工坊的水平面 (x, z) = 我们的 (x, y)；ry（弧度）↔ rot_z（度）；s 乘到 scale 上
  r（占地半径）= 实例包围盒水平两边的平均半宽（没有包围盒时用资产尺寸 × scale）
  单位：工坊的能力参数（例如间距松弛每个最多移动 0.35）按它的场景单位写；我们的场景从 2 米到 200 米都有，
  先按「占地半径中位数 = 0.5」换算成工坊单位，执行完再换回米。
  海天远景、岛体（地形）不进物件层：它们不是「物件」，当障碍会把岛上所有东西推出去。
"""
from __future__ import annotations

import math
import statistics


def layout_rows(layout: list[dict], spec: dict, instances: dict | None = None,
                skip_assets: set | frozenset = frozenset(), locked: set | frozenset = frozenset()) -> tuple[list[dict], float]:
    """返回 (工坊 layout 行, 单位换算 unit：工坊 1 单位 = unit 米)。"""
    assets = {a["id"]: a for a in spec.get("assets", [])}
    rows = []
    for x in layout:
        aid = x.get("asset")
        if aid in skip_assets or aid not in assets:
            continue
        b = (instances or {}).get(x["id"]) or {}
        if b.get("size"):
            sx, sy = float(b["size"][0]), float(b["size"][1])
        else:
            s = float(x.get("scale", 1.0) or 1.0)
            sz = assets[aid].get("size_m") or [1, 1, 1]
            sx, sy = float(sz[0]) * s, float(sz[1]) * s
        at = x.get("at") or [0, 0, 0]
        rows.append({"id": x["id"], "kind": aid, "name": assets[aid].get("name", aid), "x": float(at[0]), "z": float(at[1]),
                     "r": max(1e-3, (sx + sy) / 4), "edit": {"dx": 0, "dy": 0, "dz": 0, "ry": 0, "s": 1, "hidden": False},
                     "locked": x["id"] in locked})
    unit = (statistics.median(r["r"] for r in rows) / 0.5) if rows else 1.0
    for r in rows:
        r["x"], r["z"], r["r"] = r["x"] / unit, r["z"] / unit, r["r"] / unit
    return rows, unit


def apply_edits(layout: list[dict], edits: dict, unit: float) -> list[dict]:
    out = []
    for x in layout:
        e = edits.get(x["id"])
        if not e:
            out.append(x)
            continue
        at = list(x.get("at") or [0, 0, 0])
        at[0] = round(at[0] + float(e.get("dx", 0)) * unit, 4)
        at[1] = round(at[1] + float(e.get("dz", 0)) * unit, 4)
        at[2] = round(at[2] + float(e.get("dy", 0)) * unit, 4)
        out.append({**x, "at": at, "rot_z": round(float(x.get("rot_z", 0)) + math.degrees(float(e.get("ry", 0))), 2),
                    "scale": round(float(x.get("scale", 1.0) or 1.0) * float(e.get("s", 1)), 4)})
    return out
