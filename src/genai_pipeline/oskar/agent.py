"""Oskar 顾问 Agent：指挥 Bonsai（或路由到的模型）+ 确定性引擎完成一个场景。

流程：SAM 3 客观数据 → 读图问卷 → 场景程序（校验 + 带错误重试）→ 引擎构建 + 硬检查 → 渲染 →
客观相似度 → 评审（只列大形差异 + 受限编辑）→ 客观分升且硬检查不退步才接受，否则回滚。
脚本 scripts/oskar_bonsai.py 与流水线 oskar 模式共用本模块。
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Callable

from .. import vision
from ..llm import LLM, user_message
from . import palette as PAL
from . import prompts as P
from .program import validate_program

SAM_PROMPTS = ["sea stack", "rock", "tower", "dome", "castle wall", "building", "waterfall", "crystal tower", "tree"]


async def sam_facts(target: str, out: Path) -> str:
    seg = await vision.segment_instances(target, SAM_PROMPTS, out / "sam")
    if not seg:
        return ""
    rows = []
    for prompt, insts in seg.items():
        for x in insts:
            if x["area"] < 0.003:
                continue
            b = x["box"]
            rows.append({"what": prompt, "x": round((b[0] + b[2]) / 2, 3), "width": round(b[2] - b[0], 3),
                         "top": round(b[1], 3), "bottom": round(b[3], 3), "height": round(b[3] - b[1], 3),
                         "score": x["score"]})
    rows.sort(key=lambda r: -r["height"] * r["width"])
    rows = sorted(rows[:14], key=lambda r: r["x"])
    return ("客观数据（SAM 3 实例分割，图像坐标 0..1，x=水平中心，top/bottom=上下边，y 向下增大）：\n"
            + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows))


def _review_ok(o: Any) -> str | None:
    return None if isinstance(o, dict) and isinstance(o.get("diffs"), list) else "需要 {\"diffs\": [...]}"


async def plan(target: str, out: Path, *, model: str = "main", use_sam: bool = True, auto_palette: bool = False,
               emit: Callable[[str, str, Any], None] | None = None, style_rules: str = "") -> dict:
    """读图与规划：SAM 客观数据 → 问卷 → 场景程序（校验失败带错误重试）。"""
    out.mkdir(parents=True, exist_ok=True)
    say = emit or (lambda kind, msg, data=None: None)
    llm = LLM(model)
    sysmsg = {"role": "system", "content": P.SYSTEM + style_rules}
    facts = ""
    if use_sam:
        say("info", "SAM 3 分割目标图，提取客观数据…")
        facts = await sam_facts(target, out)
        (out / "facts.txt").write_text(facts)
    say("info", "读图问卷…")
    read = await llm.json([sysmsg, user_message(P.READ.format(facts=facts), [target], max_side=1280)],
                          thinking_budget=2048, retries=2)
    (out / "read.json").write_text(json.dumps(read, ensure_ascii=False, indent=1))
    say("result", f"读图：{len((read or {}).get('main_subjects') or [])} 个主体，类型 {(read or {}).get('scene_type')}", read)
    say("info", "写场景程序…")
    program = await llm.json([sysmsg, user_message(P.PLAN.format(read=json.dumps(read, ensure_ascii=False), facts=facts,
                                                                 examples=P.examples_text()), [target], max_side=1024)],
                             thinking_budget=3072, retries=2, validate=validate_program)
    if isinstance(read, dict) and read.get("scene_type"):
        program["scene_type"] = read["scene_type"]    # 决定哪些硬检查适用（如台面层次只查海蚀岩柱）
    if use_sam and (auto_palette or program.get("palette") not in PAL.PALETTES):
        say("info", "从目标图按语义区域取色（自动配色）…")
        seg = await vision.segment_instances(target, PAL.AUTO_PROMPTS, out / "sam_palette")
        if seg:
            found = PAL.region_colors(target, seg)
            base = program.get("palette") if program.get("palette") in PAL.PALETTES else "citadel"
            program["palette_base"] = base
            program["palette"] = PAL.auto_palette(found, base)
            (out / "palette.json").write_text(json.dumps({"found": found, "palette": program["palette"]}, ensure_ascii=False, indent=1))
            say("result", f"自动配色：取到 {len(found)} 个区域主色（{'、'.join(found)}），其余沿用 {base}", found)
    (out / "program_plan.json").write_text(json.dumps(program, ensure_ascii=False, indent=1))
    return {"facts": facts, "read": read, "program": program}


async def evaluate_program(hub, target: str, pg: dict, tag: str, views=("hero", "hero_left")) -> dict:
    """确定性构建 + 硬检查 + 固定机位日光渲染 + 客观相似度（多场景导演与单场景迭代共用）。"""
    pal = pg.get("palette")
    res = await hub.call("blender_build_oskar", {"program": pg, "palette": pal if isinstance(pal, str) else ""})
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    r = await hub.call("blender_render", {"views": list(views), "label": tag, "lighting": "day"})
    imgs = r.get("images", [])
    m = await vision.objective_similarity(target, imgs)
    return {"ok": True, "checks": res.get("checks"), "notes": res.get("notes"), "images": imgs,
            "checks_ok": all(c["passed"] for c in res.get("checks", []) if c.get("required")),
            "metrics": {k: m[k] for k in ("dino", "siglip")} if m else None,
            "objective": vision.objective_index(m), "tiles": res.get("tiles"), "triangles": res.get("triangles"),
            "walk": res.get("walk"), "paths": res.get("paths"), "size": res.get("size")}


async def run(target: str, hub, out: Path, *, model: str = "main", rounds: int = 3, use_sam: bool = True,
              program: dict | None = None, emit: Callable[[str, str, Any], None] | None = None,
              views=("hero", "hero_left"), on_round: Callable[[dict], None] | None = None, style_rules: str = "",
              patience: int = 3, palette_lock: dict | None = None) -> dict:
    """返回 {"read", "facts", "history", "best"}；best 含 program / images / metrics / checks。
    palette_lock：配色环节从目标图测得的颜色（{"palette": {角色: 色值}, "tint_mode": ...}）。
    每次构建前都压到程序上：读图取色与评审改动都改不了它。"""
    out.mkdir(parents=True, exist_ok=True)
    say = emit or (lambda kind, msg, data=None: None)
    llm = LLM(model)
    sysmsg = {"role": "system", "content": P.SYSTEM + style_rules}
    result: dict[str, Any] = {"steps": []}
    facts, read = "", None
    if program is None:
        t0 = time.time()
        pl = await plan(target, out, model=model, use_sam=use_sam, emit=emit, style_rules=style_rules)
        facts, read, program = pl["facts"], pl["read"], pl["program"]
        result["steps"].append({"step": "read+plan", "sec": round(time.time() - t0)})
    result.update({"read": read, "facts": facts})

    def lock(pg: dict) -> dict:
        if not palette_lock:
            return pg
        cur = pg.get("palette")
        base = PAL.resolve(cur, pg.get("palette_base")) if cur else PAL.resolve(None)
        pg["palette"] = {**base, **(palette_lock.get("palette") or {})}
        if palette_lock.get("tint_mode"):
            pg["tint_mode"] = palette_lock["tint_mode"]
        if palette_lock.get("forest") is not None:   # 林地比例也按目标图实测（岛顶上植被像素的占比）
            for it in pg.get("items") or []:
                if it.get("type") == "terrain":
                    it["forest"] = palette_lock["forest"]
        fit = palette_lock.get("fit")
        if fit:
            # 实测形体（深度定位）：格子大小、层高、收分、岩面细节、海面都按目标图，读图与评审改不了
            pg["grid"] = {**(pg.get("grid") or {}), **fit["grid"]}
            for key in ("level_h", "sea_level", "batter", "rock_detail"):
                pg[key] = fit[key]
            if fit.get("facet"):
                pg["facet"] = fit["facet"]
            else:
                pg.pop("facet", None)
            items = list(pg.get("items") or [])
            if fit.get("strip"):
                # 场景里房子、灯塔、码头、树等各有自己的资产：地形只做地形本身，不再自带一套建筑（否则重复）
                items = [it for it in items if it.get("type") in ("terrain", "path", "waterfall", "clear")]
                pg["vegetation"] = "none"   # 树、灌木也各有资产：引擎不再自动成林、不种灌丛
                pg["sea_plate"] = False      # 海面也有自己的资产（远景层）：地形不再自带一块六边形海面
            ter = next((it for it in items if it.get("type") == "terrain"), None)
            if ter is None:
                ter = {"type": "terrain"}
            items = [ter] + [it for it in items if it is not ter]
            ter.update(at=[0, 0], r=0.85, top=fit["sea_level"] + fit["levels"])
            ter.pop("peaks", None)
            ter.pop("plateaus", None)
            if fit.get("forest") is not None:
                ter["forest"] = fit["forest"]
            for it in items:   # 通路高度不能超出实测顶面
                if it.get("type") == "path":
                    lv = it.get("levels", it.get("level"))
                    cap = ter["top"]
                    if isinstance(lv, list):
                        it["levels"] = [min(int(x), cap) for x in lv]
                    elif isinstance(lv, (int, float)):
                        it["level"] = min(int(lv), cap)
            pg["items"] = items
        return pg

    def built(pg: dict) -> dict:
        """真正交给引擎构建的程序：锁定后再把实测高度图、台地放进地形项（不进评审提示词，太长且不许改）。"""
        pg = lock(pg)
        fit = (palette_lock or {}).get("fit")
        if not fit:
            return pg
        b = copy.deepcopy(pg)
        ter = next(it for it in b["items"] if it.get("type") == "terrain")
        ter["relief"] = fit["relief"]
        ter["plateaus"] = fit["plateaus"]
        return b

    async def evaluate(pg: dict, tag: str) -> dict:
        return await evaluate_program(hub, target, built(pg), tag, views)

    history = []
    cur = await evaluate(program, "r0")
    (out / "program_r0.json").write_text(json.dumps(program, ensure_ascii=False, indent=1))
    history.append({"round": 0, "program": program, "accepted": True, **cur})
    if on_round:
        on_round(history[-1])
    best = history[-1]
    stale = 0   # 连续未被接受的轮数：轮数加大后靠它提前收手，避免空转
    for rnd in range(1, rounds + 1):
        if stale >= max(1, patience):
            say("review", f"连续 {stale} 轮没有被接受的修改，提前停止（共 {rnd - 1} 轮）")
            break
        t0 = time.time()
        mt = ""
        if best.get("metrics"):
            mt = f"当前客观相似度 DINO {best['metrics']['dino']:.3f} / SigLIP {best['metrics']['siglip']:.3f}。"
        fails = [c for c in (best.get("checks") or []) if c.get("required") and not c["passed"]]
        if fails:
            mt += "硬检查未过（优先修）：" + "；".join(f"{c['name']} {c['detail']}" for c in fails)
        if best.get("notes"):
            mt += "引擎自动修正：" + "；".join(best["notes"][:4])
        numbered = json.dumps([{"item": i, **it} for i, it in enumerate(best["program"]["items"])], ensure_ascii=False)
        try:
            rev = await llm.json([sysmsg, user_message(P.REVIEW.format(metrics=mt, program=numbered),
                                                       [target] + (best.get("images") or [])[:2], max_side=768)],
                                 thinking_budget=2048, retries=2, validate=_review_ok)
        except Exception as e:
            history.append({"round": rnd, "error": f"评审失败：{str(e)[:200]}"})
            stale += 1
            continue
        edits = []
        for d in rev["diffs"][:3]:   # 兼容扁平写法与旧的 {"edit": {...}} 写法
            if isinstance(d, dict):
                e = d.get("edit") if isinstance(d.get("edit"), dict) else d
                if e.get("op"):
                    edits.append(e)
        newp, elog = P.apply_edits(best["program"], edits)
        err = validate_program(newp)
        if err:
            history.append({"round": rnd, "review": rev, "edit_log": elog, "error": err})
            stale += 1
            continue
        res = await evaluate(newp, f"r{rnd}")
        (out / f"program_r{rnd}.json").write_text(json.dumps(newp, ensure_ascii=False, indent=1))
        # 接受规则：硬检查优先——修好了硬检查（当前最佳没过）直接接受；硬检查状态相同时，客观分须升 0.3 以上
        if not res.get("ok"):
            accepted = False
        elif res["checks_ok"] != bool(best.get("checks_ok")):
            accepted = res["checks_ok"]
        else:
            accepted = (res.get("objective") or 0) > (best.get("objective") or 0) + 0.3
        history.append({"round": rnd, "program": newp, "review": rev, "edit_log": elog, "accepted": accepted,
                        "sec": round(time.time() - t0), **res})
        if on_round:
            on_round(history[-1])
        if accepted:
            best = history[-1]
            stale = 0
        else:
            stale += 1
    result["history"] = history
    result["best"] = best
    best["built"] = built(best["program"])
    return result
