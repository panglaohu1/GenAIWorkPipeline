"""通用场景层：Blender 场景的材质色表 ↔ 系统工坊的 materials，跑 system-workshop/gscene 的能力
（风格统一 / 主色归并、整体色调、套用风格包（莫里斯色板））和它的 verify.js（数值、整体明度、明暗层次、颜色数）。

对应关系：
  材质 = 「资产/材质名」：authored（设定色，sRGB 0..1）、当前配色系数 fix（线性乘数，配色闭环给的）、像素数
  materials 行 {id: "资产/材质", name, base: 设定色 hex, color: 当前色 hex（设定色 × 系数）, uses: 像素数}
  能力给回 {id: 新颜色 hex} → 换算成新的线性乘数（新色 ÷ 设定色，逐通道），并进配色系数表，Blender 里照常用 _cfix 节点改色。
"""
from __future__ import annotations


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _hex(rgb) -> str:
    return "#" + "".join(f"{round(max(0, min(1, v)) * 255):02x}" for v in rgb)


def _rgb(h: str) -> list[float]:
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]


SKIP_MATERIALS = frozenset({"osk_outline"})


def material_rows(materials: list[dict], roots: dict, fixes: dict | None = None,
                  skip_assets: set | frozenset = frozenset()) -> list[dict]:
    """material_stats 的行（按实例）→ 每个「资产/材质」一行。"""
    acc: dict[str, dict] = {}
    for m in materials:
        asset = roots.get(m.get("root"))
        name = m.get("material")
        if not asset or asset in skip_assets or name in SKIP_MATERIALS or not m.get("authored"):
            continue
        key = f"{asset}/{name}"
        row = acc.setdefault(key, {"id": key, "name": name, "asset": asset, "authored": [float(v) for v in m["authored"][:3]],
                                   "uses": 0})
        row["uses"] += int(m.get("px", 0))
    out = []
    for key, row in acc.items():
        f = ((fixes or {}).get(row["asset"]) or {}).get(row["name"]) or [1.0, 1.0, 1.0]
        cur = [_srgb(_lin(a) * k) for a, k in zip(row["authored"], f)]
        out.append({"id": key, "name": row["name"], "base": _hex(row["authored"]), "color": _hex(cur),
                    "uses": max(1, row["uses"])})
    return sorted(out, key=lambda r: -r["uses"])


def colors_to_fixes(rows: list[dict], colors: dict, fixes: dict | None = None) -> dict:
    """能力给的新颜色 → 新的配色系数表（只改有新颜色的材质，其它保留）。"""
    out = {a: {m: list(v) for m, v in d.items()} for a, d in (fixes or {}).items()}
    by_id = {r["id"]: r for r in rows}
    for key, hexc in colors.items():
        r = by_id.get(key)
        if not r:
            continue
        asset, name = key.split("/", 1)
        base = _rgb(r["base"])
        new = _rgb(hexc)
        out.setdefault(asset, {})[name] = [round(max(0.05, min(6.0, _lin(n) / max(_lin(b), 1e-4))), 4)
                                           for n, b in zip(new, base)]
    return out
