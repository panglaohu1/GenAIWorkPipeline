"""星球组装：把已验收的场景产线贴到同一颗小行星上，由 Oskar 用地势把它们连起来，并生成可玩的星球游戏。

游戏组装（mode=assembly）选择“组装方式：星球”时的阶段：
  assembly_plan（固定来源）→ planet_compose（Blender：重建场景、地势连接、贴球、导出）
  → planet_godot（Godot：小老虎探险 + 每站一封信 + 环球有轨电车，试运行核对）→ journey（人工试玩确认）→ deliver
- Oskar 场景来源用它保存的场景程序重建（得到可站立点 / 通路，并套用当前的 Oskar 山体色场）；
  其他来源直接用固定的 GLB（没有可站立点时，信件与连接按岛的尺寸估计）。
- 地势连接：读两站相对一侧的岛缘高度 → Oskar 选地势（阶梯台地 / 山脊垭口 / 低地地峡）→ 同一引擎生成，两端融进岛缘。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .mcp_hub import MCPHub
from .oskar import director as D
from .oskar import planet as PL

PLANET_MAIN_GD = ('extends "res://addons/pipeline/planet_game.gd"\n'
                  "## 星球游戏：小老虎探险收集各站的信，F 上下有轨电车，车上 C 切换司机视角。玩法逻辑在 planet_game.gd。\n")


def _tool(ctx, name: str, what: str) -> None:
    agent = (ctx.options.get("style_profile") or {}).get("name") or "Oskar"
    ctx.emit("tool_call", f"{agent} → {name}：{what}", {})


def _levels(ctx) -> list[dict]:
    return [p for p in ctx.options["assembly"]["parts"] if p["role"] == "level"]


def _source_program(part: dict) -> dict | None:
    """旧版组装清单没有记录场景程序：从来源任务的 state.json 读 Oskar 程序（只读，不改来源）。"""
    from . import assembly
    if part.get("mode") != "oskar":
        return None
    try:
        st = json.loads((assembly.root() / part["source_id"] / "state.json").read_text())
    except (OSError, ValueError):
        return None
    prog = (st.get("oskar") or {}).get("program")
    return prog if isinstance(prog, dict) and prog.get("items") else None


async def compose(ctx) -> None:
    from . import assembly
    from .pipeline import route
    assembly.validate(ctx.options, ctx.dir)
    levels = _levels(ctx)
    if not levels:
        raise RuntimeError("星球组装至少需要一个关卡场景")
    names = {f"s{i + 1}": {"name": p["title"], "role": "level"} for i, p in enumerate(levels)}
    order = list(names)
    recs: dict[str, dict] = {}
    out = ctx.dir / "planet"
    out.mkdir(parents=True, exist_ok=True)
    direction = D.resolve_direction((ctx.options.get("style_profile") or {}).get("direction"))
    async with MCPHub(ctx.dir, servers=["blender"]) as hub:
        # 1. 各站：Oskar 场景按程序重建（可站立点、通路、当前色场），其他来源用固定 GLB
        for sid, part in zip(order, levels):
            prog = part.get("program") or _source_program(part)
            if isinstance(prog, dict) and prog.get("items"):
                _tool(ctx, "Blender.build_oskar", f"重建「{part['title']}」（Oskar 场景程序，山体按色场从山顶往下铺色）")
                b = await hub.call("blender_build_oskar", {"program": prog})
                if b.get("ok") is False:
                    raise RuntimeError(f"{part['title']} 重建失败：{b.get('error')}")
                e = await hub.call("blender_export_glb", {"asset_name": f"planet_{sid}"})
                if not e.get("ok"):
                    raise RuntimeError(f"{part['title']} 导出失败：{e.get('error') or e.get('problems')}")
                recs[sid] = {"glb": e["path"], "walk": b.get("walk") or [], "paths": b.get("paths") or [],
                             "size": e.get("info", {}).get("size") or b.get("size"), "program": prog}
            else:
                recs[sid] = {"glb": part["assets"][0]["glb"], "walk": [], "paths": [], "size": None}
        missing = [sid for sid in order if not recs[sid].get("size")]
        if missing:   # 只有 GLB 的来源：先按旅程带自动贴一次，取回尺寸
            probe = await hub.call("blender_compose_planet", {"items": [{"id": s, "glb": recs[s]["glb"]} for s in order],
                                                               "links": []})
            for s in missing:
                recs[s]["size"] = (probe.get("sizes") or {}).get(s) or [40, 40, 12]
        sizes = {s: recs[s]["size"] for s in order}
        lay = PL.plan(order, sizes, climax=order[len(order) // 2] if len(order) > 2 else None, gap=6.0,
                      spacing=float(ctx.opt("planet_spacing", 1.6)))   # 已有场景通常较大：星球放大一些，单站弯曲更小
        # 2. 地势连接：Oskar 读岛缘、选地势；同一引擎生成
        progs = {s: recs[s].get("program") or {} for s in order}
        lv = next((p for p in progs.values() if p), {})
        specs = PL.link_specs(lay, {s: recs[s]["walk"] for s in order}, level_h=float(lv.get("level_h", 2.0)),
                              sea_level=int(lv.get("sea_level", 1) if lv.get("sea_level") is not None else 1))
        plan: dict[str, dict] = {}
        if specs:
            _tool(ctx, "LLM.plan_links", f"读 {len(specs)} 段连接两端的岛缘高度与间隙，为每段选地势")
            bible = {"title": ctx.options["assembly"]["title"], "scenes": [{"id": s, "name": names[s]["name"],
                                                                           "link_to_next": "auto"} for s in order]}
            try:
                plan = await D.plan_links(specs, bible=bible, direction=direction, model=route(ctx, "plan"),
                                          on_delta=ctx.delta_emitter())
            except Exception as exc:   # noqa: BLE001  模型不可用时按岛缘高度规则选地势
                ctx.emit("warn", f"地势连接规划没有拿到 Oskar 的判断（{str(exc)[:80]}），按岛缘高度规则选地势")
                plan = {sp["id"]: {**PL.default_link(sp), "why": "规则：按两端岛缘高度"} for sp in specs}
        links = []
        for i, sp in enumerate(specs):
            choice = plan.get(sp["id"]) or PL.default_link(sp)
            pa = progs.get(sp["a"]) or {}
            prog = PL.link_program(sp, choice, palette=pa.get("palette"), palette_base=pa.get("palette_base"),
                                   vegetation=pa.get("vegetation"), seed=i + 1)
            kind = PL.LINK_KINDS[prog["_link"]["kind"]]
            _tool(ctx, "Blender.build_oskar", f"生成连接地形 {names[sp['a']]['name']} → {names[sp['b']]['name']}（{kind}）")
            b = await hub.call("blender_build_oskar", {"program": prog})
            if b.get("ok") is False:
                raise RuntimeError(f"连接地形构建失败：{b.get('error')}")
            key = hashlib.sha1(json.dumps(prog, sort_keys=True).encode()).hexdigest()[:6]
            e = await hub.call("blender_export_glb", {"asset_name": f"planet_{sp['id']}_{key}"})
            if not e.get("ok"):
                raise RuntimeError(f"连接地形导出失败：{e.get('error') or e.get('problems')}")
            links.append({**sp, **prog["_link"], "why": choice.get("why", ""), "glb": e["path"],
                          "walk": b.get("walk") or [], "paths": b.get("paths") or []})
            ctx.emit("info", f"连接 {names[sp['a']]['name']} → {names[sp['b']]['name']}：{kind}，宽 {prog['_link']['width']:g} 米"
                             + (f" — {choice['why']}" if choice.get("why") else ""))
        # 3. 贴到球面
        items = [{"id": x["id"], "glb": recs[x["id"]]["glb"], "lat": x["lat"], "lon": x["lon"], "yaw": x["yaw"], "r": x["r"]}
                 for x in lay["scenes"]]
        items += [{"id": t["id"], "glb": t["glb"], "lat": t["lat"], "lon": t["lon"], "yaw": t["yaw"],
                   "r": max(4.0, t["gap"] / 2), "kind": "link"} for t in links]
        _tool(ctx, "Blender.compose_planet", f"把 {len(order)} 个场景与 {len(links)} 段地势连接贴到半径 {lay['radius']:.0f} 米的星球上")
        comp = await hub.call("blender_compose_planet", {"items": items, "radius": lay["radius"], "links": []})
        if comp.get("errors") or comp.get("ok") is False:
            raise RuntimeError("星球整合失败：" + "；".join((comp.get("errors") or ["未知错误"])[:3]))
        r = await hub.call("blender_render", {"views": ["hero", "hero_left"], "label": "planet", "lighting": "day"})
        images = r.get("images", [])
        exp = await hub.call("blender_export_glb", {"asset_name": "oskar_atlas"})
    if not exp.get("ok"):
        raise RuntimeError(f"星球导出失败：{exp.get('error') or exp.get('problems')}")
    for p in images:
        ctx.artifact("render", p, "星球组装全景", asset="_planet")
    walk, paths, stops = PL.journey(recs, lay, names, links=links)
    game = PL.game_layout(recs, lay, names)
    ctx.state["planet"] = {"layout": lay, "names": names, "glb": exp["path"], "images": images,
                           "export_size": exp.get("info", {}).get("size"),
                           "terrain_links": [{k: v for k, v in t.items() if k not in ("walk", "paths")} for t in links],
                           "walk": walk, "paths": paths, "journey": stops, "game": game}
    (out / "planet_layout.json").write_text(json.dumps({"layout": lay, "journey": stops,
                                                        "links": ctx.state["planet"]["terrain_links"]},
                                                       ensure_ascii=False, indent=1))
    ctx.artifact("glb", exp["path"], f"{ctx.options['assembly']['title']} · 星球.glb", asset="oskar_atlas",
                 triangles=exp.get("info", {}).get("triangles"))
    ctx.save()
    ctx.emit("result", f"星球组装：{len(order)} 站贴到半径 {lay['radius']:.0f} 米的星球上，"
                       f"{len(links)} 段地势连接（{'、'.join(PL.LINK_KINDS[t['kind']] for t in links) or '无'}），"
                       f"{len(game['letters'])} 封信，电车线路 {len(game['tram']['route'])} 个轨道点")


async def godot(ctx) -> None:
    from .blender import look
    from .godot import oskar_scene as OG
    from .pipeline import _godot_project
    pl = ctx.state["planet"]
    g = pl["game"]
    views = {("hero_left" if "hero_left" in p else "hero"): p for p in pl.get("images") or []}
    extra = {"planet": {"center": [0.0, 0.0, 0.0], "radius": pl["layout"]["radius"]},
             "no_collision": ["oskar_sea", "oskar_props", "oskar_ocean"],
             "clouds": {"count": 14, "speed": 1.2, "wind": [1.0, 0.0, 0.35], "in_showcase": False},
             "journey": [{**s, **{k: look.to_godot(s[k]) for k in ("center", "entry", "exit")}} for s in pl["journey"]],
             "game": {"letters": [{**L, "pos": look.to_godot(L["pos"])} for L in g["letters"]],
                      "fog_density": g.get("fog_density", 0.0),
                      "tram": {**g["tram"], "route": [look.to_godot(u) for u in g["tram"]["route"]]}}}
    _tool(ctx, "Godot.import + run", "接入星球：碰撞、小老虎、每站一封信、环球有轨电车；试运行截图并与 Blender 核对")
    async with MCPHub(ctx.dir, godot_project=_godot_project(ctx), servers=["godot"]) as hub:
        res = await OG.setup_and_verify(hub, _godot_project(ctx), blender_views=views or None,
                                        blender_size=pl.get("export_size"), walk=pl.get("walk"), paths=pl.get("paths"),
                                        emit=lambda k, m, d=None: ctx.emit(k, m, d), glb_name="oskar_atlas.glb",
                                        extra=extra, main_gd=PLANET_MAIN_GD)
    for p, label in zip(res["shots"], res["labels"]):
        ctx.artifact("screenshot", p, f"Godot · 星球 · {label}", asset="oskar_atlas", shot=label)
    pl["godot"] = {k: res[k] for k in ("ok", "checks", "shots", "labels", "match", "fps", "project")}
    ctx.save()
    fails = [c for c in res["checks"] if c["required"] and not c["passed"]]
    ctx.emit("review", "Godot 星球接入：" + ("全部核对通过" if not fails else "未通过：" + "；".join(
        f"{c['name']}（{c['detail']}）" for c in fails)), {"checks": res["checks"], "match": res["match"]})
    if fails:
        raise RuntimeError("Godot 星球接入核对未通过：" + "；".join(f"{c['name']} {c['detail']}" for c in fails))
    ctx.emit("result", f"星球游戏已就绪：用 Godot 打开 {Path(res['project'])}，运行即可——"
                       "WASD 移动、空格跳、拖动鼠标转视角；靠近电车按 F 上车，车上按 C 切换司机视角，再按 F 下车")
