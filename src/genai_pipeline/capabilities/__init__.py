"""能力层：本管线自己的能力 + TigerInBamboo 能力库（REACode 体素能力、系统工坊的物件 / 通用场景 / 球系统能力）。

  contract.py   能力契约（meta / match / run / verify）、Registry、Router、经验、Watchdog、TierAgent
  native.py     本管线的能力：实测天空盒、布光闭环、相机俯角闭环、配色闭环、拆分自检
  bridge.py     调外部能力代码（Node 子进程跑 bridge.mjs，能力代码原样从 TigerInBamboo import）
  voxel.py      Oskar 角点状态 ↔ 体素 World（石拱、悬垂、海中石柱、隧道、灯塔…），在 Blender 工作进程里执行
  objects.py    组装布局 ↔ 物件布局（同类尺寸变化、朝向变化、间距松弛）
  gscene.py     材质色表 ↔ materials（主色归并、整体色调、风格包）

管线里怎么用（pipeline.stage_compose / blender build_oskar）：
  1. 组装阶段先按「能力计划」跑本管线的能力（compose_plan，默认 天空盒 → 布光 → 俯角 → 配色），每步 match → run → verify。
  2. 每轮布局定下来后跑物件层：Router 按改造方向（需求描述 + 风格规则）+ 管线里已启用的（config capabilities.object_selected，
     默认「间距松弛」）选能力 → 在布局副本上执行 → 工坊的 verify.js 验证 → 不是 fail 才用新布局。
  3. 配色闭环之后跑通用场景层（只在 Router 选中时，例如需求里写了「莫里斯」「挂毯」→ 套用风格包）：执行 → 工坊验证
     → 同机位渲染比色差和相似度（变差就放弃）→ 并进配色系数。
  4. Oskar 引擎建地形 / 场景程序时跑体素层（plan 写在 运行目录/capabilities/plan.json，build_oskar 读取）。
每步写经验：data/experience/<层>.jsonl。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .. import config
from . import bridge, gscene as GS, objects as OBJ
from .contract import Capability, ExperienceStore, Registry, TierAgent, params_for, route, summarize_checks
from .native import COMPOSE_PLAN, NATIVE

__all__ = ["Capability", "Registry", "TierAgent", "route", "params_for", "summarize_checks", "catalog",
           "native_registry", "run_native_plan", "object_pass", "gscene_pass", "experience", "write_run_plan"]


def experience(tier: str) -> ExperienceStore:
    return ExperienceStore(config.resolve(config.get("capabilities.experience_dir", "data/experience")) / f"{tier}.jsonl")


def native_registry(tier: str | None = None) -> Registry:
    reg = Registry(tier or "native")
    for c in NATIVE:
        if tier is None or c.meta.get("tier") == tier:
            reg.add(c)
    return reg


async def catalog() -> dict:
    """全部能力（给控制台 / 文档）：本管线的 + 外部能力库的（外部不可用时写明原因）。"""
    out: dict[str, Any] = {"native": [c.describe() for c in NATIVE]}
    ok, why = bridge.available()
    if not ok:
        out["external"] = {"error": why}
        return out
    try:
        out["external"] = await bridge.call("describe", {})
    except bridge.BridgeError as e:
        out["external"] = {"error": str(e)[:500]}
    out["roots"] = bridge.roots()
    return out


def _skip_note(ctx, tier: str, why: str) -> None:
    """外部能力不可用时每个任务只提示一次（不打断管线）。"""
    seen = ctx.state.setdefault("capability_notes", [])
    if why not in seen:
        seen.append(why)
        ctx.emit("warn", f"外部能力（{tier} 等）这次没用上：{why}")


def _ev_emitter(ctx) -> Callable[[dict], None]:
    def emit(e: dict) -> None:
        if e.get("type") == "recover":
            ctx.emit("warn", f"能力 Watchdog：{e.get('why')}，重建后重试", e)
    return emit


async def run_native_plan(ctx, sub: SimpleNamespace, plan: tuple | list = COMPOSE_PLAN) -> list[dict]:
    """按计划执行本管线的能力：match（能不能做、为什么）→ run → verify（fail 的结果撤回）→ 记经验。"""
    reg = native_registry("scene3d")
    exp = experience("scene3d")
    report = []
    for cid in plan:
        cap = reg.get(cid)
        if cap is None:
            ctx.emit("warn", f"能力计划里的 {cid} 没有登记，跳过")
            continue
        params = params_for(cap.meta, (ctx.opt("capability_params", {}) or {}).get(cid))
        targets = cap.match(sub, params) or []
        if not targets or cap.run is None:
            report.append({"id": cid, "status": "no-target"})
            if cap.meta.get("skip_note") and cap.meta["skip_note"](sub):
                ctx.emit("info", f"能力 {cap.meta['name']}（{cid}）：跳过 —— {cap.meta['skip_note'](sub)}")
            continue
        res = await cap.run(sub, targets, params)
        ver = cap.verify(sub, res) if cap.verify else summarize_checks([])
        if ver["status"] == "fail" and cap.revert:
            cap.revert(sub, res)
        row = {"id": cid, "status": "applied" if res.get("applied") else "no-target", "verify": ver["status"],
               "reason": targets[0].get("reason", "")}
        report.append(row)
        exp.add({"tier": "scene3d", "run": ctx.id, "steps": [{"id": cid, "status": row["status"]}], "verify": ver["status"],
                 "why": "；".join(f"{c['name']}：{c.get('detail', '')}" for c in ver["checks"] if c["status"] == "fail"),
                 "outcome": "discarded" if ver["status"] == "fail" else "committed"})
        ctx.emit("info" if ver["status"] != "fail" else "warn",
                 f"能力 {cap.meta['name']}（{cid}）：{targets[0].get('reason', '')} → 验证 {ver['summary']}"
                 + ("，已撤回" if ver["status"] == "fail" and cap.revert else ""), {"capability": cid, "verify": ver})
    return report


def _goal(ctx, spec: dict) -> str:
    """改造方向：需求标题 + 描述 + 场景描述 + 显式写的改造方向（Router 只认关键词，不调 LLM）。
    风格代理的通用规则不算进来：那些是每个场景都带的套话（「清晰色块」「风格统一」），会让每次都命中同几个能力。"""
    parts = [ctx.run.get("title", ""), ctx.run.get("brief", ""), (spec.get("scene") or {}).get("description", ""),
             str(ctx.opt("capability_goal", "") or "")]
    return "\n".join(p for p in parts if p)


async def object_pass(ctx, spec: dict, layout: list[dict], instances: dict | None = None) -> tuple[list[dict], dict]:
    """物件层（系统工坊 object 能力）：返回 (新布局, 报告)。验证 fail 或外部不可用时返回原布局。"""
    if not ctx.opt("object_capabilities", True):
        return layout, {"status": "off"}
    ok, why = bridge.available()
    if not ok:
        _skip_note(ctx, "物件 / 场景风格", why)
        return layout, {"status": "skipped", "why": why}
    models = ctx.state.get("models") or {}
    skip = {a["id"] for a in spec.get("assets", []) if (models.get(a["id"]) or {}).get("mode") in ("backdrop", "terrain")
            or a.get("method") in ("backdrop", "terrain") or a.get("kind") == "environment"}
    rows, unit = OBJ.layout_rows(layout, spec, instances, skip_assets=skip,
                                 locked=set(ctx.opt("object_locked", []) or []))
    if len(rows) < 2:
        return layout, {"status": "no-target", "why": "可调的物件不到 2 个"}
    selected = list(ctx.opt("object_capabilities_selected", None) or config.get("capabilities.object_selected", ["object.spacing-relax"]))
    emit = _ev_emitter(ctx)
    try:
        desc = await bridge.call("describe", {}, emit=emit)
        caps = desc.get("object") if isinstance(desc.get("object"), list) else []
        agent = TierAgent("object", caps=caps, experience=experience("object"), emit=emit, hooks={
            "scan": lambda L: bridge.call("object.scan", {"layout": L}, emit=emit),
            "run": lambda L, steps: bridge.call("object.run", {"layout": L, "steps": steps}, emit=emit),
            "verify": lambda L, res, steps: bridge.call("object.verify", {"layout": L, "edits": res["edits"], "steps": steps}, emit=emit),
            "commit": lambda cand: cand.result})
        await agent.scan(rows)
        routed = agent.route(_goal(ctx, spec), selected)
        metas = {c["id"]: c for c in caps}
        # 参数写全（默认值也写上）：工坊的验证按步骤参数算位移预算等，缺了会按最严的算
        steps = [{"id": r["id"], "params": params_for(metas[r["id"]], (ctx.opt("capability_params", {}) or {}).get(r["id"]))}
                 for r in routed]
        if not steps:
            return layout, {"status": "no-route", "scanned": {m["id"]: m["count"] for m in agent.scan_result or []}}
        cand = await agent.generate(rows, steps, goal="组装布局")
    except bridge.BridgeError as e:
        ctx.emit("warn", f"物件能力没跑成，沿用原布局：{str(e)[:200]}")
        return layout, {"status": "error", "why": str(e)[:500]}
    ver = cand.verification
    rep = {"status": ver["status"], "steps": [s["id"] for s in steps], "routed": routed, "report": cand.report,
           "verify": ver, "unit_m": round(unit, 4)}
    if ver["status"] == "fail":
        agent.discard(cand, "验证未通过")
        ctx.emit("warn", f"物件能力 {'、'.join(rep['steps'])}：验证不通过（{ver['summary']}），沿用原布局", rep)
        return layout, rep
    await agent.commit(cand)
    edits = cand.result.get("edits") or {}
    ctx.emit("info", f"物件能力 {'、'.join(rep['steps'])}：改了 {len(edits)} 个实例；验证 {ver['summary']}"
             + "；" + "；".join(r["why"] for r in routed[:2]), rep)
    return OBJ.apply_edits(layout, edits, unit), rep


async def gscene_pass(ctx, sub: SimpleNamespace) -> dict:
    """通用场景层（系统工坊 gscene 能力）：Router 选中才跑。工坊验证 + 同机位渲染比色差 / 相似度都不变差才并进配色系数。"""
    from .. import pipeline as P, vision
    if not ctx.opt("gscene_capabilities", True):
        return {"status": "off"}
    ok, why = bridge.available()
    if not ok:
        _skip_note(ctx, "物件 / 场景风格", why)
        return {"status": "skipped", "why": why}
    spec, rec, hub, cam = sub.spec, sub.rec, sub.hub, sub.cam
    emit = _ev_emitter(ctx)
    backdrops = {a["id"] for a in spec["assets"] if ((ctx.state.get("models") or {}).get(a["id"]) or {}).get("mode") == "backdrop"}
    fix0 = (rec.get("color_fix_best") or {}).get("fix") or rec.get("color_fix") or {}
    rargs = P._rargs(spec, rec)
    await hub.call("blender_recolor", {"fixes": fix0, "roots": sub.roots})
    r0 = await hub.call("blender_render", {**rargs, "views": ["ref"], "label": "gscene_0", "camera": cam})
    img0 = (r0.get("images") or [None])[0]
    if not img0:
        return {"status": "no-render"}
    st = await hub.call("blender_material_stats", {"image": img0, "camera": cam})
    rows = GS.material_rows(st.get("materials") or [], sub.roots, fix0, skip_assets=backdrops)
    selected = list(ctx.opt("gscene_capabilities_selected", None) or config.get("capabilities.gscene_selected", []))
    try:
        desc = await bridge.call("describe", {}, emit=emit)
        caps = desc.get("gscene") if isinstance(desc.get("gscene"), list) else []
        agent = TierAgent("gscene", caps=caps, experience=experience("gscene"), emit=emit, hooks={
            "scan": lambda M: bridge.call("gscene.scan", {"materials": M}, emit=emit),
            "run": lambda M, steps: bridge.call("gscene.run", {"materials": M, "steps": steps}, emit=emit),
            "verify": lambda M, res, steps: bridge.call("gscene.verify", {"materials": M, "colors": res["colors"]}, emit=emit),
            "commit": lambda cand: cand.result})
        await agent.scan(rows)
        routed = agent.route(_goal(ctx, spec), selected)
        if not routed:
            return {"status": "no-route"}
        metas = {c["id"]: c for c in caps}
        steps = [{"id": r["id"], "params": params_for(metas[r["id"]], (ctx.opt("capability_params", {}) or {}).get(r["id"]))}
                 for r in routed]
        cand = await agent.generate(rows, steps, goal="场景风格")
    except bridge.BridgeError as e:
        ctx.emit("warn", f"场景风格能力没跑成：{str(e)[:200]}")
        return {"status": "error", "why": str(e)[:500]}
    rep = {"status": cand.verification["status"], "steps": [s["id"] for s in steps], "routed": routed,
           "verify": cand.verification, "report": cand.report}
    if cand.verification["status"] == "fail":
        agent.discard(cand, "工坊验证未通过")
        ctx.emit("warn", f"场景风格能力 {'、'.join(rep['steps'])}：验证不通过（{cand.verification['summary']}），不采用", rep)
        return rep
    fix1 = GS.colors_to_fixes(rows, cand.result.get("colors") or {}, fix0)
    await hub.call("blender_recolor", {"fixes": fix1, "roots": sub.roots})
    r1 = await hub.call("blender_render", {**rargs, "views": ["ref"], "label": "gscene_1", "camera": cam})
    img1 = (r1.get("images") or [None])[0]
    c0 = await P._color_check(ctx, spec, sub.target, img0, hub, cam, sub.roots)
    c1 = await P._color_check(ctx, spec, sub.target, img1, hub, cam, sub.roots) if img1 else None
    m0 = await vision.objective_similarity(sub.target, [img0])
    m1 = await vision.objective_similarity(sub.target, [img1]) if img1 else None
    de0, de1 = (c0 or {}).get("mean_delta_e"), (c1 or {}).get("mean_delta_e")
    d0, d1 = (m0 or {}).get("dino"), (m1 or {}).get("dino")
    tol_de, tol_dino = float(ctx.opt("gscene_de_tol", 1.0)), float(ctx.opt("gscene_dino_tol", 0.01))
    worse = (de0 is not None and (de1 is None or de1 > de0 + tol_de)) or (d0 is not None and (d1 is None or d1 < d0 - tol_dino))
    rep.update({"de": [de0, de1], "dino": [d0, d1]})
    if worse:
        await hub.call("blender_recolor", {"fixes": fix0, "roots": sub.roots})
        agent.discard(cand, f"渲染比对变差：ΔE {de0}→{de1}，DINO {d0}→{d1}")
        rep["status"] = "discarded"
        ctx.emit("info", f"场景风格能力 {'、'.join(rep['steps'])}：工坊验证 {cand.verification['summary']}，"
                         f"但同机位比对变差（ΔE {de0}→{de1}，DINO {d0}→{d1}），不采用", rep)
        return rep
    await agent.commit(cand)
    rec["color_fix"] = fix1
    rec["color_fix_best"] = {**(rec.get("color_fix_best") or {}), "fix": fix1, "de": de1, "gscene": rep["steps"]}
    ctx.emit("info", f"场景风格能力 {'、'.join(rep['steps'])}：采用（ΔE {de0}→{de1}，DINO {d0}→{d1}；"
                     f"{'；'.join(r['why'] for r in routed[:2])}）", rep)
    return rep


def write_run_plan(ctx, spec: dict) -> Path | None:
    """体素层计划写进 运行目录/capabilities/plan.json；Blender 的 build_oskar 读取（Oskar 程序本身不用改）。"""
    if not ctx.opt("voxel_capabilities", True):
        return None
    d = ctx.dir / "capabilities"
    d.mkdir(parents=True, exist_ok=True)
    plan = {"voxel": {"goal": _goal(ctx, spec),
                      "selected": list(ctx.opt("voxel_capabilities_selected", None) or config.get("capabilities.voxel_selected", [])),
                      "params": ctx.opt("capability_params", {}) or {}}}
    p = d / "plan.json"
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=1))
    return p
