"""流水线编排：阶段定义、运行状态持久化、审批关卡、串行运行队列。

模式:
  game  : plan → concept → model → assemble → review → deliver
  asset : plan → concept → model → deliver
每次运行的所有文件在 runs/<run_id>/，状态在 runs/<run_id>/state.json，可从任意阶段重跑。
"""
from __future__ import annotations
import re

import asyncio
import json
import math
import os
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import config, jev, prompts, vision, style_agents, consultation, assembly, world_atlas, planet_assembly, teams, team_research
from .agent import Cancelled, RunControl, run_agent
from .llm import LLM, user_message
from .mcp_hub import MCPHub
from .store import Store
from .compute import compute
from .quality import assess
from . import capabilities as CAPS
from types import SimpleNamespace

STAGES = {
    "assembly": ["assembly_plan", "assemble", "review", "journey", "deliver"],
    "assembly_planet": ["assembly_plan", "planet_compose", "planet_godot", "journey", "deliver"],
    "consult": ["consult"],
    "learn": ["learn"],
    "game": ["plan", "concept", "world", "model", "assemble", "review", "deliver"],   # 世界由 Oskar 引擎按关键画面生成
    "asset": ["plan", "concept", "model", "deliver"],
    "scene": ["decompose", "model", "compose", "deliver"],
    # 图生 3D 场景：参考图 → 拆资产（标出哪些用图生 3D、哪些用蓝图建模）→ Hunyuan3D 生成核心资产 + Blender 整理着色
    # → 多机位组装与评审迭代（借鉴 Tripo + Blender 的做法，全部本地算力）
    "scene3d": ["decompose", "model", "compose", "deliver"],
    "oskar": ["oskar_plan", "oskar_build", "oskar_godot", "deliver"],  # Oskar 风格：Agent + Bonsai 写场景程序 → WFC 引擎构建与迭代 → Godot 展示工程
    # Oskar 导演的多场景世界：世界圣经 → 逐站目标图（导演评审）→ 逐场景趋近 → 旅程弧线整合 → Godot
    "atlas": ["atlas_brief", "atlas_targets", "atlas_scenes", "atlas_compose", "atlas_godot", "deliver"],
    # 团队（Greg team 等）做完整游戏：议事出计划（人工确认）→ 按计划分工建造 → 成员复盘与效能结算
    "team_game": ["team_plaza", "plan", "concept", "model", "assemble", "review", "team_retro", "deliver"],
}
STAGE_LABELS = {"planet_compose": "星球整合", "planet_godot": "星球游戏接入", "assembly_plan": "成果编排", "journey": "完整流程试玩确认", "plan": "方案规划", "concept": "目标概念图", "model": "Blender 建模", "assemble": "Godot 组装",
                "review": "试玩验收", "deliver": "交付打包", "decompose": "拆分子资产", "compose": "场景组装",
                "oskar_plan": "读图与场景程序", "oskar_build": "引擎构建与迭代", "oskar_godot": "Godot 场景接入",
                "world": "Oskar 世界", "learn": "研究与风格提炼", "consult": "顾问审阅与修改提案",
                "atlas_brief": "世界构想", "atlas_targets": "导演出目标图", "atlas_scenes": "逐场景趋近",
                "atlas_compose": "世界整合", "atlas_godot": "Godot 世界接入",
                "team_plaza": "团队议事与计划", "team_research": "团队调研（证据分级）", "team_retro": "团队复盘与效能结算"}


class RunContext:
    def __init__(self, store: Store, run: dict, control: RunControl) -> None:
        self.store = store
        self.run = run
        self.id = run["id"]
        self.dir = config.resolve(config.get("pipeline.runs_dir", "runs")) / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.control = control
        self.options = run["options"]
        self.stage = ""
        self._state_path = self.dir / "state.json"
        self.state: dict[str, Any] = json.loads(self._state_path.read_text()) if self._state_path.exists() else {}

    def save(self) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))
        tmp.replace(self._state_path)

    def opt(self, key: str, default: Any = None) -> Any:
        v = self.options.get(key)
        return v if v not in (None, "") else config.get(f"pipeline.{key}", default)

    def emit(self, kind: str, message: str, data: Any = None) -> None:
        self.store.event(self.id, self.stage, kind, message, data)

    def artifact(self, kind: str, path: Path | str, label: str = "", **meta) -> str:
        return self.store.add_artifact(self.id, self.stage, kind, str(path), label, meta)

    def delta_emitter(self) -> Callable[[str, str], None]:
        buf = {"reasoning": "", "content": ""}

        def on_delta(kind: str, text: str) -> None:
            buf[kind] += text
            if len(buf[kind]) > 300 or "\n" in text:
                self.emit("stream", "", {"kind": kind, "text": buf[kind]})
                buf[kind] = ""
        return on_delta

    async def wait_approval(self, what: str, handle_request: Callable[[dict], Awaitable[None]] | None = None) -> dict:
        """在控制台人工确认前阻塞；期间可处理 regenerate 等请求。"""
        if what not in (self.opt("approval_gates", []) or []):
            return {}
        self.store.set_stage(self.id, self.stage, "waiting", {"waiting_for": what})
        self.emit("approval", f"等待人工确认：{STAGE_LABELS.get(what, what)}（在控制台选择后点“确认继续”）", {"gate": what})
        with self.control.lock:
            self.store.update_run(self.id, status="paused" if self.control.paused else "waiting")
        while True:
            await self.control.checkpoint()
            if self.control.requests:
                req = self.control.requests.pop(0)
                if handle_request:
                    self.store.set_stage(self.id, self.stage, "running")
                    await handle_request(req)
                    self.store.set_stage(self.id, self.stage, "waiting", {"waiting_for": what})
                continue
            with self.control.lock:
                if not self.control.paused and not self.control.cancelled and what in self.control.approvals:
                    payload = self.control.approvals.pop(what)
                    self.store.update_run(self.id, status="running")
                    self.store.set_stage(self.id, self.stage, "running", {"waiting_for": None})
                    self.emit("approved", "已确认，继续执行", payload)
                    return payload
            await asyncio.sleep(0.5)


# ───────────────────────────── 阶段实现 ─────────────────────────────

async def stage_plan(ctx: RunContext) -> None:
    mode = ctx.run["mode"]
    brief = ctx.run["brief"]
    max_assets = int(ctx.opt("max_assets", 4)) if mode == "game" else 1  # 单资产模式只产出一个资产（整张目标图对应它）
    refs = [a["path"] for a in ctx.store.artifacts(ctx.id) if a["kind"] == "reference"]
    if mode == "game":
        system = prompts.PLAN_SYSTEM.format(max_assets=max_assets)
        hint = prompts.PLAN_SCHEMA_HINT
    else:
        system = prompts.ASSET_PLAN_SYSTEM
        hint = prompts.ASSET_SCHEMA_HINT

    def validate(o: Any) -> str | None:
        if not isinstance(o, dict):
            return "需要 JSON 对象"
        assets = o.get("assets")
        if not isinstance(assets, list) or not assets:
            return "assets 必须是非空数组"
        if len(assets) > max_assets:
            return f"资产最多 {max_assets} 个"
        ids = set()
        for a in assets:
            for k in ("id", "name", "description", "image_prompt"):
                if not a.get(k):
                    return f"资产缺少字段 {k}"
            if a["id"] in ids:
                return f"资产 id 重复: {a['id']}"
            ids.add(a["id"])
            size = a.get("size_m")
            if not (isinstance(size, list) and len(size) == 3 and all(isinstance(x, (int, float)) for x in size)):
                return f"{a['id']}.size_m 必须是 3 个数字"
        if mode == "game":
            for k in ("scene", "gameplay", "acceptance", "camera"):
                if k not in o:
                    return f"缺少字段 {k}"
        return None

    msg = user_message(f"需求：\n{brief}\n\n{hint}", refs or None)
    ctx.emit("info", "LLM 正在规划方案…")
    spec = await LLM(route(ctx, "plan")).json(
        [{"role": "system", "content": system + style_agents.rules_text(ctx.options.get("style_profile"), "plan") + consultation.revision_rules(ctx.options)}, msg], on_delta=ctx.delta_emitter(), thinking_budget=6144,
        validate=validate, schema_hint=hint)
    floors = {"character": 8000, "vehicle": 3000, "aircraft": 3000, "building": 3000, "environment": 3000,
              "prop": 1500, "collectible": 800}
    for a in spec["assets"]:
        a.setdefault("kind", "prop")
        a.setdefault("role", "decor")
        # 规划模型常给出不现实的低面数（如角色 900），按类型设下限，避免硬性检查反复卡住
        a["poly_budget"] = max(int(a.get("poly_budget") or 8000), floors.get(a["kind"], 1500))
    ctx.state["spec"] = spec
    ctx.save()
    (ctx.dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2))
    ctx.artifact("spec", ctx.dir / "spec.json", spec.get("title", "方案"))
    if spec.get("title"):
        ctx.store.update_run(ctx.id, title=spec["title"])
    ctx.emit("result", f"方案：{spec.get('title', '')}，{len(spec['assets'])} 个资产", {"spec": spec})


async def stage_concept(ctx: RunContext) -> None:
    spec = ctx.state["spec"]
    n = int(ctx.opt("concept_candidates", 3))
    style = spec.get("art_style", "low poly, stylized")
    backend = ctx.options.get("image_backend") or config.get("image.default_backend")
    concepts: dict[str, dict] = ctx.state.setdefault("concepts", {})

    async def gen_for(asset_id: str, prompt: str, count: int) -> None:
        out_dir = ctx.dir / "concept" / asset_id
        out_dir.mkdir(parents=True, exist_ok=True)
        entry = concepts.setdefault(asset_id, {"candidates": [], "prompt": prompt})
        entry["prompt"] = prompt
        start = len(entry["candidates"])
        for k in range(count):
            await ctx.control.checkpoint()
            idx = start + k
            out = out_dir / f"cand_{idx:02d}.png"
            full = prompt if asset_id == "_key_art" else f"{prompt}, {style}, {vision.CONCEPT_SUFFIX}"
            ctx.emit("info", f"生成概念图 {asset_id} #{idx}（{backend}）")
            res = await vision.generate_image(full, out, backend=backend, seed=1000 + idx * 7919)
            aid = ctx.artifact("concept", out, f"{asset_id} #{idx}", asset=asset_id, index=idx, prompt=full,
                               seconds=res.get("seconds"), backend=res.get("backend"))
            entry["candidates"].append({"path": str(out), "artifact": aid})
            ctx.save()
        cands = [c["path"] for c in entry["candidates"]]
        subject = prompt if asset_id == "_key_art" else next(a["description"] for a in spec["assets"]
                                                             if a["id"] == asset_id)
        if len(cands) > 1:
            pick = await vision.choose_best(cands, subject, route(ctx, "observe"), ctx.delta_emitter())
            best = pick["best"]
            ctx.emit("result", f"{asset_id}: 选择候选 #{best} — {pick.get('reason', '')}", pick)
        else:
            best = 0
        entry["selected"] = best
        for i, c in enumerate(entry["candidates"]):
            ctx.store.update_artifact_meta(c["artifact"], selected=(i == best))
        ctx.save()

    refs = [a for a in ctx.store.artifacts(ctx.id) if a["kind"] == "reference"]
    if ctx.options.get("reference_as_target") and refs:
        # 用户提供的参考图直接作为目标图（例如 TigerMessenger 已认可的概念图），不再生成
        for a in spec["assets"]:
            if a["id"] not in concepts:
                concepts[a["id"]] = {"prompt": "(用户参考图)", "selected": 0,
                                     "candidates": [{"path": r["path"], "artifact": r["id"]} for r in refs]}
                ctx.emit("info", f"{a['name']}：使用用户上传的参考图作为目标图")
        ctx.save()
    for a in spec["assets"]:
        if a["id"] not in concepts or not concepts[a["id"]].get("candidates"):
            await gen_for(a["id"], a["image_prompt"], n)
    if ctx.run["mode"] == "game" and "_key_art" not in concepts:
        kp = spec.get("scene", {}).get("key_art_prompt", "")
        await gen_for("_key_art", f"{kp}, isometric diorama island world in the style of Townscaper and Bad North, "
                                  f"low poly, irregular grid, soft colors, {style}, wide shot", max(1, n - 1))

    async def handle(req: dict) -> None:
        if req.get("type") == "regenerate":
            aid = req["asset"]
            prompt = req.get("prompt") or concepts.get(aid, {}).get("prompt")
            await gen_for(aid, prompt, int(req.get("count", 2)))

    payload = await ctx.wait_approval("concept", handle)
    for asset_id, art_id in (payload.get("selections") or {}).items():
        entry = concepts.get(asset_id)
        if entry:
            for i, c in enumerate(entry["candidates"]):
                if c["artifact"] == art_id:
                    entry["selected"] = i
                ctx.store.update_artifact_meta(c["artifact"], selected=(c["artifact"] == art_id))
    ctx.save()


def _target_image(ctx: RunContext, asset_id: str) -> str:
    e = ctx.state["concepts"][asset_id]
    return e["candidates"][e.get("selected", 0)]["path"]


async def stage_model(ctx: RunContext) -> None:
    spec = ctx.state["spec"]
    models: dict[str, dict] = ctx.state.setdefault("models", {})
    scene3d = ctx.run.get("mode") == "scene3d"
    max_rounds = scene3d_limits(ctx.options)["blueprint_rounds"] if scene3d else int(ctx.opt("model_max_rounds", 6))
    pass_score = float(ctx.opt("model_pass_score", 7.5))
    only = ctx.options.get("only_assets")
    async with MCPHub(ctx.dir, servers=["blender"]) as hub:
        for a in spec["assets"]:
            if only and a["id"] not in only:
                continue
            m = models.get(a["id"], {})
            if m.get("glb") and a["id"] not in (ctx.options.get("revisit_assets") or []):
                if m.get("mode") == "blueprint" and m.get("rounds"):
                    bi = _best_index(m["rounds"], a.get("kind") in SYMMETRIC_KINDS)
                    b = m["rounds"][bi]
                    if b["round"] != m.get("exported_round"):  # 旧逻辑导出的是漂移后的冠军：按最佳轮重新导出
                        await hub.call("blender_build_blueprint", {"blueprint": b["blueprint"]})
                        await hub.call("blender_run_python", {"code": "H.ground_to_origin()"})
                        exp = await _export_asset(ctx, hub, a["id"])
                        if exp.get("ok"):
                            m.update(best=bi, exported_round=b["round"], final_score=b["score"], glb=exp["path"])
                            ctx.artifact("glb", exp["path"], f"{a['name']}.glb（按最佳第 {b['round']} 轮重新导出）",
                                         asset=a["id"], score=b["score"], triangles=exp.get("info", {}).get("triangles"),
                                         size=exp.get("info", {}).get("size"))
                            ctx.emit("info", f"{a['name']}：改为导出最佳的第 {b['round']} 轮（{b['score']:.1f} 分）")
                            ctx.save()
                ctx.emit("info", f"{a['name']} 已有 GLB，跳过")
                continue
            mode = ctx.options.get("model_mode") or config.get("pipeline.model_mode", "blueprint")
            if scene3d:
                tools = ((ctx.options.get("style_profile") or {}).get("config") or {}).get("tools")
                method, why = asset_method(a, tools)
                if method != a.get("method"):
                    ctx.emit("info", f"{a['name']}：{why}")
                a["method"] = method
            if a.get("method") == "image3d" and scene3d and ctx.opt("engine_props", True) \
                    and await _model_asset_engine(ctx, hub, a, spec):
                a["method"] = "engine"   # 引擎构件做成了：不再走图生 3D
            elif a.get("method") == "image3d":
                await _model_asset_image3d(ctx, hub, a, spec, pass_score)
            elif a.get("method") == "terrain":
                await _model_asset_terrain(ctx, hub, a)
            elif a.get("method") == "backdrop":
                await _model_asset_backdrop(ctx, hub, a)
            elif a.get("method") == "skip":
                models[a["id"]] = {"mode": "skip"}
                ctx.save()
            elif mode == "agent" or ctx.options.get("source_model"):  # 迭代已有资产需要在原模型上改，用 agent 模式
                await _model_asset(ctx, hub, a, spec, max_rounds, pass_score)
            else:
                await _model_asset_blueprint(ctx, hub, a, spec, max_rounds, pass_score)


def scene3d_limits(options: dict | None) -> dict:
    """图生 3D 场景的轮数与候选数：运行选项 > config [shape3d] > 默认值。"""
    o, c = options or {}, config.get("shape3d", {}) or {}
    def pick(opt_key, cfg_key, default, lo, hi):
        v = o.get(opt_key)
        v = c.get(cfg_key, default) if v in (None, "") else v
        return max(lo, min(hi, int(v)))
    return {"candidates": pick("shape3d_candidates", "candidates", 2, 1, 6),
            "blueprint_rounds": pick("model_max_rounds", "blueprint_rounds", 3, 1, 12),
            "patience": pick("plateau_patience", "patience", 2, 1, 12),
            "compose_rounds": pick("compose_max_rounds", "compose_rounds", 4, 1, 20),
            "terrain_rounds": pick("terrain_rounds", "terrain_rounds", 2, 0, 8)}


# 拆分模型常把天空、云海也标成普通环境件：按名称关键词兜底（中英文）
SKY_WORDS = ("sky", "sun_disk", "sun disk", "sunset sky", "天空", "太阳", "落日", "夕阳")
FAR_WORDS = ("cloud", "sea of clouds", "ocean", "horizon", "distant", "far mountain", "云海", "云层", "海面", "大海",
             "地平线", "远山", "远景")


def asset_method(a: dict, tools: list[str] | None = None) -> tuple[str, str]:
    """图生 3D 场景里每个子资产走哪条路（返回 (method, 原因)）：
    - backdrop：远景层（云海、海面、远处地平线）→ Blender 铺一片起伏团块，颜色从参考图按深度取样；
    - terrain：大面积地面/台地/岛体（环境件、水平边 ≥ 8 米）→ Oskar 引擎生成地形（收分、台缘、草皮、岸线）；
    - image3d：道具、角色、建筑、植物 → Hunyuan3D 图生 3D；Agent 没启用 shape3d 工具时改走蓝图。"""
    size = a.get("size_m") or [1, 1, 1]
    # 只看名字（id / name），不看描述：描述里常顺带写“伸入海面”“停在海上”，会把码头、小船误判成远景
    words = " ".join(str(a.get(k, "")) for k in ("id", "name")).lower()
    loose = a.get("kind") in (None, "", "environment", "backdrop", "sky")
    if (a.get("measured") or {}).get("on_plane"):
        # 深度定位实测它躺在水面 / 地面上：是海面或地面，不是天空（名字里带「sky」也一样，比如「海天远景」）
        return "backdrop", "远景层实测躺在水面上：按目标图的水面区域铺海面"
    if loose and any(w in words for w in SKY_WORDS):
        return "skip", "天空 / 太阳不建模：组装渲染用参考图天空的颜色作背景"
    if a.get("kind") == "backdrop" or (loose and any(w in words for w in FAR_WORDS)):
        return "backdrop", "远景层（云海 / 海面 / 远处地平线）在 Blender 里按参考图铺一片起伏团块"
    if a.get("kind") == "environment" and max(size[:2]) >= 8:
        return "terrain", "大面积地面 / 台地 / 岛体交给 Oskar 引擎生成地形"
    if tools and "shape3d" not in tools:
        return "blueprint", "当前 Agent 未启用「Hunyuan3D 图生 3D」工具，改用蓝图建模"
    return "image3d", "道具、角色、建筑、植物交给 Hunyuan3D 图生 3D"


async def _english_prompt(ctx: RunContext, text: str, focus: str, fallback: str = "") -> str:
    """把中文需求/描述写成一句英文生图提示（FLUX 只懂英文）。失败时退回 fallback 或原文。"""
    try:
        obj = await LLM(route(ctx, "plan")).json([
            {"role": "system", "content": f"把下面的内容写成一句英文生图提示，重点描述{focus}；不要出现文字、标题、水印。只输出 JSON。"},
            {"role": "user", "content": text + '\n输出 {"prompt": "english"}'}],
            thinking_budget=512, validate=lambda o: None if isinstance(o, dict) and str(o.get("prompt", "")).strip() else "需要 prompt")
        return obj["prompt"].strip()
    except Exception:
        return fallback or text


def _terrain_palette_lock(spec: dict) -> dict | None:
    """配色环节的实测色 → Oskar 地形调色板（崖壁、草、水、天、植被）与上色方式。没有实测就返回 None（沿用引擎默认）。"""
    pm = spec.get("palette_measured") or {}
    rock = pm.get("terrain_rock") or pm.get("terrain")
    if not rock:
        return None
    pal = {"rock": rock["base"], "rock_alt": rock["lit"], "rock_dark": rock["shade"], "rock_top": rock["lit"]}
    top = pm.get("terrain_top")
    if top:
        pal.update(grass=top["base"], grass_alt=top["lit"], grass_dark=top["shade"])
    water = pm.get("water")
    if water:
        pal.update(sea=water["base"], sea_deep=water["shade"], foam=water["lit"], water=water["lit"])
    if pm.get("sky"):
        pal["sky"] = pm["sky"]["base"]
    import colorsys

    def hs(h):   # 色相（度）与饱和度
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        hh, _, ss = colorsys.rgb_to_hls(r, g, b)
        return hh * 360, ss
    # 植被色：不按资产名字猜，取主色落在绿色色相（60°–170°）里、饱和度最高的那个物件
    plants = sorted((v for v in (pm.get("assets") or {}).values()
                     if v.get("base") and 60 <= hs(v["base"])[0] <= 170),
                    key=lambda v: -hs(v["base"])[1])
    if plants:
        pal.update(canopy=plants[0]["base"], shrub=plants[0]["shade"], cypress=plants[0]["shade"])
    # 岛顶区域空（掩码过严 / 被植被盖住）时，用绿色物件或地形色团里偏绿的一档，避免草=None 进引擎
    if "grass" not in pal:
        if plants:
            pal.update(grass=plants[0]["base"], grass_alt=plants[0].get("lit") or plants[0]["base"],
                       grass_dark=plants[0].get("shade") or plants[0]["base"])
        else:
            clusters = (pm.get("terrain") or {}).get("clusters") or []
            greenish = sorted(
                (c for c in clusters if c.get("hex") and 60 <= hs(c["hex"])[0] <= 170),
                key=lambda c: -hs(c["hex"])[1])
            if greenish:
                g = greenish[0]["hex"]
                pal.update(grass=g, grass_alt=g, grass_dark=g)
            elif rock:
                pal.update(grass=rock.get("lit") or rock["base"],
                           grass_alt=rock.get("lit") or rock["base"],
                           grass_dark=rock.get("shade") or rock["base"])
    out = {"palette": pal, "tint_mode": "target-" + pm.get("terrain_mode", "mono")}
    top_px = (top or {}).get("pixels") or 0
    if top_px:   # 岛顶上植被的覆盖率 → 地形 WFC 的林地权重（目标图只有几丛灌木时就不要长成一片森林）
        cover = sum(v.get("pixels", 0) for v in plants) / top_px
        out["forest"] = round(max(0.0, min(0.4, cover)), 3)
    return out


def _terrain_fit(spec: dict, a: dict) -> dict | None:
    """深度定位测得的地形形体 → Oskar 引擎参数（引擎的程序坐标：岛半径 0.85 × S，S = 网格半径 × 2 × 格边长 × 0.85）。
    - 轮廓、每格高度：俯视高度图（relief.h），引擎按它定每格瓦片的层数，崖从水面直上；
    - 格子大小：崖面单个平整面的实测宽度 ≈ 六边形格的边长（边长 ≈ 0.62 × 格边长）→ 网格半径；
    - 层高：实测岛高按格子尺度分 2–8 层；收分：实测崖面外倾角；岩面细节：实测面内粗糙度；
    - 立在岛顶的物件位置 → 引擎预留平地（台地）；其他物件各有资产，地形不再自带建筑。"""
    import math
    rel = a.get("relief")
    if not rel or not rel.get("h"):
        return None
    half = float(rel["half_m"])
    th = float(rel["size_m"][2])
    cl = rel.get("cliff") or {}
    cell = 2.0
    radius = 7
    if cl.get("facet_m"):
        radius = int(max(3, min(9, round(0.62 * half / (1.445 * float(cl["facet_m"]))))))
    R = 0.85 * radius * 2 * cell * 0.85
    k = half / R                      # 世界米 / 程序米
    prog_h = th / k
    levels = int(max(2, min(8, round(prog_h / cell))))
    level_h = round(prog_h / levels, 3)
    tilt = max(0.0, float(cl.get("tilt_deg") or 0.0))
    batter = round(min(level_h, level_h * math.tan(math.radians(tilt))), 3)
    # 粗糙度按法线 30° 分组量得：分组本身带来约 7° 的离散，超出部分才是岩面真实的起伏
    rough = cl.get("roughness_deg")
    detail = 1.0 if rough is None else round(max(0.0, min(1.0, (float(rough) - 7.0) / 10.0)), 2)
    # 块面起伏：位移场的坡度就是崖面偏转的正切。波长取三块崖面宽；幅度让坡度均方根 ≈ tan(实测偏角)
    # （三个正弦取平均，均方根约为峰值的 0.41），但峰值坡度不超过 0.8——再陡相邻面会折叠穿插，
    # 描边壳从缝里露出来成黑色裂口（实测幅度 0.86、波长 3.1 时出现）
    facet = None
    if cl.get("facet_m") and cl.get("facet_dev_deg"):
        w = float(cl["facet_m"]) / k
        dev = math.radians(min(45.0, float(cl["facet_dev_deg"])))
        wave = 3 * w
        fq = 2 * math.pi / (0.8 * wave)
        amp = min(math.tan(dev) / (0.41 * 0.8 * fq), 0.8 / (0.8 * fq))
        facet = [round(amp, 3), round(wave, 3)]
    plateaus = [{"at": [round(p["at"][0] * 0.85, 4), round(p["at"][1] * 0.85, 4)], "r": round(p["r"] * 0.85, 4),
                 "top": int(round(p["h"] * levels)), "asset": p.get("asset")} for p in rel.get("plateaus") or []]
    solid = [x for x in spec.get("assets") or [] if x["id"] != a["id"] and x.get("method") not in ("backdrop", "skip")
             and x.get("kind") != "backdrop"]
    return {"relief": {"h": rel["h"], **({"bare": rel["bare"]} if rel.get("bare") else {})}, "plateaus": plateaus, "grid": {"radius": radius, "cell": cell},
            "level_h": level_h, "levels": levels, "sea_level": 0, "batter": batter, "rock_detail": detail,
            "facet": facet,
            "strip": bool(solid), **({"forest": 0.0} if solid else {}),
            "measured": {"facet_m": cl.get("facet_m"), "tilt_deg": cl.get("tilt_deg"), "roughness_deg": rough,
                         "facet_dev_deg": cl.get("facet_dev_deg"),
                         "land_cells": rel.get("land_cells")}}


async def _model_asset_terrain(ctx: RunContext, hub: MCPHub, a: dict) -> None:
    """大面积地形交给 Oskar 引擎：按该资产的参考裁剪读图写场景程序 → 构建 → 受限迭代（客观分升才接受）
    → 缩放到 size_m → 导出。引擎自带收分、台缘草唇、灌丛带、岸线波纹与描边。"""
    from .oskar import agent as OA
    target = _target_image(ctx, a["id"])
    out = ctx.dir / "terrain" / a["id"]
    rounds = scene3d_limits(ctx.options)["terrain_rounds"]
    ctx.emit("round", f"{a['name']} · Oskar 引擎生成地形（最多 {rounds} 轮修改）", {"round": 1})
    lock = _terrain_palette_lock(ctx.state.get("spec") or {})
    if lock:
        ctx.emit("info", f"{a['name']}：配色锁定为目标图实测色（崖壁 {lock['palette'].get('rock')}、草 "
                         f"{lock['palette'].get('grass')}、水 {lock['palette'].get('sea')}；{lock['tint_mode']}）")
    fit = _terrain_fit(ctx.state.get("spec") or {}, a)
    if fit:
        lock = {**(lock or {}), "fit": fit}
        ms = fit["measured"]
        ctx.emit("info", f"{a['name']}：形体按目标图实测——高度图 {ms.get('land_cells')} 格、崖面单块宽 {ms.get('facet_m')} m"
                         f"（网格半径 {fit['grid']['radius']}）、{fit['levels']} 层、外倾 {ms.get('tilt_deg')}°、"
                         f"岩面细节 {fit['rock_detail']}、块面偏角 {ms.get('facet_dev_deg')}°、预留平地 {len(fit['plateaus'])} 处"
                         + ("；房子 / 码头 / 树等由各自资产负责，地形不再自带" if fit["strip"] else ""))
    CAPS.write_run_plan(ctx, ctx.state.get("spec") or {})   # 体素层能力（REACode）：build_oskar 读取
    res = await OA.run(target, hub, out, model=route(ctx, "plan"), rounds=rounds, use_sam=True,
                       emit=lambda kind, msg, data=None: ctx.emit(kind, f"{a['name']}：{msg}", data),
                       style_rules=style_agents.rules_text(ctx.options.get("style_profile"), "oskar"), patience=1,
                       palette_lock=lock)
    best = res["best"]
    pg = best.get("built") or best["program"]
    pal = pg.get("palette")
    await hub.call("blender_build_oskar", {"program": pg, "palette": pal if isinstance(pal, str) else ""})
    fit = await hub.call("blender_fit_to_size", {"size_m": a["size_m"]})
    # 按水平最大边等比缩放时，岛外的浪花圈也算进了宽度，岛就被缩矮了（实测 5.1 米的崖做成 3.6 米，顶上的房子悬空）。
    # 实测形体时以高度为准再缩一次：岛高 = 实测岛高，水平按同一比例（陆地的宽高比本来就是按实测造的）
    if a.get("relief") and fit.get("size") and float(fit["size"][2]) > 0.05:
        k = float(a["size_m"][2]) / float(fit["size"][2])
        if abs(k - 1) > 0.05:
            fit = await hub.call("blender_fit_to_size", {"size_m": [float(a["size_m"][0]) * k, float(a["size_m"][1]) * k,
                                                                     float(a["size_m"][2])]})
            fit["height_corrected"] = round(k, 3)
    exp = await _export_asset(ctx, hub, a["id"])
    if not exp.get("ok"):
        raise RuntimeError(f"{a['id']} 导出失败: {exp.get('problems') or exp.get('error')}")
    m = best.get("metrics") or {}
    score = round(10 * max(0.0, min(1.0, (m.get("dino") or 0) / 0.6)), 1)   # DINO 0.6 视作满分，折成 0–10 便于并列显示
    ctx.state["models"][a["id"]] = {"mode": "terrain", "glb": exp["path"], "final_score": score,
                                    "checks": best.get("checks"), "checks_ok": best.get("checks_ok"),
                                    "metrics": m, "program": pg, "fit": fit,
                                    "renders": best.get("images"), "history_rounds": len(res.get("history") or [])}
    for pth in best.get("images") or []:
        ctx.artifact("render", pth, f"{a['name']} Oskar 地形", asset=a["id"], round=1, target=target)
    ctx.artifact("glb", exp["path"], f"{a['name']}.glb（Oskar 引擎地形）", asset=a["id"], score=score,
                 triangles=exp.get("info", {}).get("triangles"), size=exp.get("info", {}).get("size"))
    ctx.emit("review", f"{a['name']}：Oskar 地形完成，DINO {m.get('dino', 0):.3f}，硬检查{'通过' if best.get('checks_ok') else '未全过'}")
    ctx.save()




ENGINE_PROP_SYSTEM = """你是 Oskar 风格低多边形场景的建模顾问。看一张物件裁剪图，决定用引擎的哪种构件做它，并给出结构参数。
尺寸、颜色都已经从目标图实测好了（下面给出），你只决定「是什么结构」，不要改尺寸，颜色只能从给出的色块里选编号。
构件：
- tower：圆塔 / 灯塔（锥台塔身，可有瞭望平台、灯室、顶盖；塔身按实测色带分段上色）
- house：单栋房屋（方盒墙身 + 坡屋顶，可在正面山墙装风车叶片）；只有一栋屋、没有方塔+拱门组合时用这个
- estate：庄园建筑群（后方方形石塔带四坡陶瓦顶 + 前方长石屋双坡顶 + 屋前石砌拱门 + 可选侧屋）。名称/描述含「塔+石屋+拱门」「庄园」「villa tower」时必须选 estate，不要退化成单栋 house
- pier：木码头 / 栈道（伸进水里的木板路，桩子入水）
- boat：小船
- tree：真树 / 柏树 / 阔叶树（树干 + 树冠：cypress 细高柏、round 圆冠、cluster 矮灌木并排）
- flower：花与前景叶丛（tulip 杯状郁金香、spike 穗状红花、blossom 小朵暖花、daisy 雏菊、foliage 亮绿叶丛）；花头用 petal、茎 stem、叶 leaf、花心 center
- none：以上都不像（交给图生 3D）
只输出 JSON。"""

ENGINE_PROP_TASK = """物件：{name}（{desc}）
实测尺寸（米，宽×深×高）：{size}
实测色团 / 色块（编号：色值，占比；可分给不同部件，如花头与叶子）：{swatches}
{bands}
输出：
{{"type": "tower|house|estate|pier|boat|tree|flower|none",
  "why": "一句话",
  "tower": {{"gallery": true, "lantern": true, "cap": "dome|cone", "taper": 0.6~1.0, "fractions": {{"body": 0.6~0.85, "lantern": 0~0.2}},
             "colors": {{"roof": 色块编号, "trim": 色块编号, "window": 色块编号}}}},
  "house": {{"roof": "gable|hip", "ridge": "x|y", "wall_frac": 0.4~0.75, "windmill": true|false, "blade_r": 米,
             "colors": {{"plaster": 色块编号, "roof": 色块编号, "sail": 色块编号}}}},
  "estate": {{"wall_frac": 0.4~0.75, "arch": true, "wing": true,
              "colors": {{"plaster": 色块编号, "roof": 色块编号, "stone": 色块编号}}}},
  "pier": {{"shape": "straight|L", "colors": {{"wood": 色块编号, "post": 色块编号}}}},
  "boat": {{"cabin": true|false, "colors": {{"hull": 色块编号, "hull_stripe": 色块编号, "cabin": 色块编号}}}},
  "tree": {{"shape": "cypress|round|cluster", "trunk_frac": 0~0.4, "count": 2~3, "colors": {{"canopy": 色块编号, "trunk": 色块编号}}}},
  "flower": {{"shape": "tulip|spike|blossom|daisy|foliage", "stem_frac": 0.15~0.5, "count": 2~4, "colors": {{"petal": 色块编号, "stem": 色块编号, "leaf": 色块编号, "center": 色块编号}}}}}}
只填与 type 对应的那一项；色块编号必须来自上面的列表，找不到合适的就省略该颜色。
注意：实测色块是从物件所在区域取的，可能混进了背后的天空、草地、水面的颜色——不要把这些当成物件本身的颜色。
红花 / 暖色花：选 flower（不要用 tree），petal 必须偏红/橙/粉；雏菊 petal 偏白、center 偏黄；绿叶丛用 foliage，leaf 必须偏绿且不要近黑。"""


def _engine_choice_error(o) -> str | None:
    """构件选择的即时校验：只给 type 不给参数时（实测：风车屋只回了 type 和 why），结构全落到默认值，风车也没了。"""
    if not isinstance(o, dict) or o.get("type") not in ("tower", "house", "estate", "pier", "boat", "tree", "flower", "none"):
        return "type 必须是 tower/house/estate/pier/boat/tree/flower/none"
    t = o["type"]
    if t != "none" and not isinstance(o.get(t), dict):
        return f"type 是 {t}，必须同时给出 \"{t}\": {{...}} 结构参数（含 colors 色块编号）"
    if t != "none" and not isinstance(o[t].get("colors"), dict):
        return f"\"{t}\" 里要有 colors：角色 → 色块编号"
    return None


def _shade_hex(h: str, k: float) -> str:
    h = h.lstrip("#")
    return "#" + "".join(f"{max(0, min(255, round(int(h[i:i + 2], 16) * k))):02X}" for i in (0, 2, 4))


def _engine_prop_spec(choice: dict, a: dict, pm: dict) -> dict | None:
    """LLM 的结构选择 + 实测尺寸 / 色块 / 色带 → 构件参数（数值全部限幅，颜色只认实测色块）。"""
    t = choice.get("type")
    if t not in ("tower", "house", "estate", "pier", "boat", "tree", "flower"):
        return None
    params = dict(choice.get(t) or {})
    # 优先消费多色团 clusters（花头 / 叶子等部件）；没有则退回 swatches
    sw = [x["hex"] for x in (pm.get("clusters") or pm.get("swatches") or [])]
    colors = {}
    for role, idx in (params.pop("colors", None) or {}).items():
        if isinstance(idx, int) and 0 <= idx < len(sw):
            colors[role] = sw[idx]
    spec = {"type": t, "size_m": [float(x) for x in a["size_m"]], "colors": colors}
    clamp = lambda v, lo, hi, d: max(lo, min(hi, float(v))) if isinstance(v, (int, float)) else d
    if t == "tower":
        spec.update(gallery=bool(params.get("gallery", True)), lantern=bool(params.get("lantern", True)),
                    cap="cone" if params.get("cap") == "cone" else "dome",
                    taper=clamp(params.get("taper"), 0.5, 1.0, 0.8),
                    fractions={"body": clamp((params.get("fractions") or {}).get("body"), 0.5, 0.9, 0.72),
                               "lantern": clamp((params.get("fractions") or {}).get("lantern"), 0.0, 0.25, 0.14)})
        if pm.get("bands"):
            spec["bands"] = pm["bands"]
        else:
            colors.setdefault("body", pm.get("base"))
    elif t == "house":
        w = float(a["size_m"][0])
        spec.update(roof="hip" if params.get("roof") == "hip" else "gable", ridge="y" if params.get("ridge") == "y" else "x",
                    wall_frac=clamp(params.get("wall_frac"), 0.35, 0.8, 0.58), windmill=bool(params.get("windmill")),
                    blade_r=clamp(params.get("blade_r"), 0.25 * w, 0.55 * w, 0.45 * w))
        colors.setdefault("plaster", pm.get("base"))
    elif t == "estate":
        spec.update(wall_frac=clamp(params.get("wall_frac"), 0.35, 0.8, 0.58),
                    arch=bool(params.get("arch", True)), wing=bool(params.get("wing", True)))
        colors.setdefault("plaster", pm.get("base"))
        if len(sw) > 1:
            colors.setdefault("roof", sw[1])
        if len(sw) > 2:
            colors.setdefault("stone", sw[2])
    elif t == "pier":
        spec.update(shape="L" if params.get("shape") == "L" else "straight")
        colors.setdefault("wood", pm.get("base"))
        if isinstance(colors.get("wood"), str) and "wood_alt" not in colors:
            # 隔条木板：没有单独实测时取实测木色略暗一档（引擎默认的深棕会和实测的浅木色排成明暗条纹）
            colors["wood_alt"] = _shade_hex(colors["wood"], 0.9)
    elif t == "boat":
        spec.update(cabin=bool(params.get("cabin", True)))
        colors.setdefault("hull", pm.get("base"))
    elif t == "tree":
        shp = params.get("shape")
        spec.update(shape=shp if shp in ("cypress", "round", "cluster") else "round",
                    trunk_frac=clamp(params.get("trunk_frac"), 0.0, 0.4, 0.15),
                    count=int(clamp(params.get("count"), 2, 3, 3)))
        colors.setdefault("canopy", pm.get("base"))
    elif t == "flower":
        shp = params.get("shape")
        if shp not in ("tulip", "spike", "blossom", "daisy", "foliage"):
            shp = "blossom"
        spec.update(shape=shp,
                    stem_frac=clamp(params.get("stem_frac"), 0.1, 0.55, 0.32),
                    count=int(clamp(params.get("count"), 2, 4, 3)),
                    florets=int(clamp(params.get("florets"), 4, 10, 7)),
                    petals=int(clamp(params.get("petals"), 6, 12, 8)),
                    leaves=bool(params.get("leaves", True)))
        # 颜色只认 LLM 按编号选的实测色团；没选的部件按色团占比顺序补（不按名字猜颜色、不改写实测色）
        rest = [h for h in sw if h not in colors.values()] or sw or [pm.get("base")]
        roles = ("leaf",) if shp == "foliage" else ("petal", "leaf", "stem", "center")
        for k, role in enumerate(r for r in roles if r not in colors):
            if rest[min(k, len(rest) - 1)]:
                colors[role] = rest[min(k, len(rest) - 1)]
    spec["colors"] = {k: v for k, v in colors.items() if isinstance(v, str)}
    return spec


async def _model_asset_engine(ctx: RunContext, hub: MCPHub, a: dict, spec: dict) -> bool:
    """物件交给 Oskar 引擎的构件（塔 / 房屋 / 码头 / 小船）：本机 LLM 看裁剪图选结构，尺寸、颜色、色带用实测。
    与岛体同一套画风，每次重跑结果稳定。LLM 判断引擎没有合适构件时返回 False，由调用方走图生 3D。"""
    crop = _target_image(ctx, a["id"])
    pm = ((spec.get("palette_measured") or {}).get("assets") or {}).get(a["id"]) or {}
    _clu = pm.get("clusters") or pm.get("swatches") or []
    sw = "；".join(f"{i}：{x['hex']}（{x['share']:.0%}）" for i, x in enumerate(_clu)) or "（无）"
    bands = ("实测色带（自上而下）：" + "，".join(f"{b['from']:.2f}–{b['to']:.2f} {b['hex']}" for b in pm["bands"])) \
        if pm.get("bands") else ""
    size = "×".join(f"{float(x):.2f}" for x in a["size_m"])
    try:
        choice = await LLM(route(ctx, "plan")).json(
            [{"role": "system", "content": ENGINE_PROP_SYSTEM},
             user_message(ENGINE_PROP_TASK.format(name=a["name"], desc=a.get("description", "")[:200], size=size,
                                                  swatches=sw, bands=bands), [str(crop)], max_side=768)],
            thinking_budget=1024, retries=2, validate=_engine_choice_error)
    except Exception as e:  # noqa: BLE001 — 选不出构件就走图生 3D
        ctx.emit("warn", f"{a['name']}：引擎构件选择失败，改走图生 3D：{str(e)[:160]}")
        return False
    prop = _engine_prop_spec(choice, a, pm)
    if prop is None:
        ctx.emit("info", f"{a['name']}：引擎没有合适的构件（{choice.get('why', '')}），改走图生 3D")
        return False
    res = await hub.call("blender_build_oskar", {"program": {"prop": prop, "bevel": 0.03, "outline": 0.035,
                                                             "soft_normals": 0}})
    if not res.get("ok"):
        ctx.emit("warn", f"{a['name']}：引擎构件构建失败，改走图生 3D：{res.get('error')}")
        return False
    r = await hub.call("blender_render", {"views": ["front", "persp"], "label": f"{a['id']}_engine", "lighting": "day"})
    imgs = r.get("images", [])
    m = await vision.objective_similarity(str(crop), imgs) if imgs else None
    exp = await _export_asset(ctx, hub, a["id"])
    if not exp.get("ok"):
        ctx.emit("warn", f"{a['name']}：引擎构件导出失败，改走图生 3D：{exp.get('problems') or exp.get('error')}")
        return False
    ctx.state["models"][a["id"]] = {"mode": "engine", "glb": exp["path"], "prop": prop, "choice": choice,
                                    "metrics": {k: m[k] for k in ("dino", "siglip")} if m else None,
                                    "renders": imgs, "size": res.get("size")}
    for pth in imgs:
        ctx.artifact("render", pth, f"{a['name']} 引擎构件（{prop['type']}）", asset=a["id"], round=1, target=str(crop))
    ctx.artifact("glb", exp["path"], f"{a['name']}.glb（Oskar 引擎构件 {prop['type']}）", asset=a["id"],
                 triangles=exp.get("info", {}).get("triangles"), size=exp.get("info", {}).get("size"))
    ctx.emit("review", f"{a['name']}：引擎构件 {prop['type']}（{choice.get('why', '')}）"
                       + (f"，DINO {m['dino']:.3f}" if m else ""))
    ctx.save()
    return True


async def _model_asset_backdrop(ctx: RunContext, hub: MCPHub, a: dict) -> None:
    """远景层：云海 / 海面 / 远处地平线，按参考裁剪的上下颜色铺一片起伏团块（不追求细节，只负责氛围与地平线）。"""
    target = _target_image(ctx, a["id"])
    mask = Path(target).with_name("mask.png")
    spec = ctx.state.get("spec") or {}
    water = ((spec.get("palette_measured") or {}).get("regions") or {}).get("water")
    scene_img = ctx.state.get("scene_target") or _target_image(ctx, "_scene")
    if (a.get("measured") or {}).get("on_plane") and water and Path(water).exists() and Path(scene_img).exists():
        # 躺在水面上的远景层（海面）：颜色取目标图里实测的水面区域（从海平线到近处），
        # 不取拆分框的裁剪——框常常只框住了天空，海就被染成天色
        target, mask = Path(scene_img), Path(water)
        ctx.emit("info", f"{a['name']}：海面颜色取目标图实测的水面区域")
    flat = bool((a.get("measured") or {}).get("on_plane"))
    res = await hub.call("blender_build_backdrop", {"size_m": a["size_m"], "image": str(target),
                                                    "mask": str(mask) if mask.exists() else "", "name": a["id"],
                                                    "flat": flat})
    if res.get("ok") is False:
        raise RuntimeError(f"{a['name']} 远景生成失败：{res.get('error')}")
    exp = await hub.call("blender_export_glb", {"asset_name": a["id"]})
    if not exp.get("ok"):
        raise RuntimeError(f"{a['id']} 导出失败: {exp.get('problems') or exp.get('error')}")
    ctx.state["models"][a["id"]] = {"mode": "backdrop", "glb": exp["path"], "backdrop": True,
                                    "build": {k: res.get(k) for k in ("puffs", "size", "colors")}}
    ctx.artifact("glb", exp["path"], f"{a['name']}.glb（远景层）", asset=a["id"],
                 triangles=exp.get("info", {}).get("triangles"), size=exp.get("info", {}).get("size"))
    ctx.emit("info", f"{a['name']}：" + ("海面平板（按远近分条上色）" if res.get("flat") else f"远景层 {res.get('puffs')} 个团块")
             + f"，色块 {res.get('colors')}")
    ctx.save()


async def _isolated_asset_image(ctx: RunContext, a: dict, spec: dict, out_dir: Path) -> Path:
    """为图生 3D 单独生成一张资产图：先让 LLM 把中文描述写成英文画面提示，再用 FLUX 出单物体、纯灰底的图。"""
    out = out_dir / "isolated.png"
    if out.exists():
        return out
    prompt = await _english_prompt(ctx, json.dumps({"name": a["name"], "description": a.get("description", ""),
                                                     "noun": a.get("sam_prompt", ""), "style": spec.get("art_style", "")},
                                                    ensure_ascii=False), "这一个物体的外形、材质、颜色、比例",
                                   fallback=f"{a.get('sam_prompt') or a['id'].replace('_', ' ')}")
    prompt = f"{prompt}, {spec.get('art_style', '')}, " + vision.CONCEPT_SUFFIX
    await vision.generate_image(prompt, out, seed=17)
    ctx.artifact("concept", out, f"{a['name']}（图生 3D 用的单物体资产图）", asset=a["id"], index=1, prompt=prompt)
    ctx.emit("info", f"{a['name']}：没有可靠的实例掩码，先单独生成单物体资产图再做图生 3D", {"prompt": prompt})
    return out


async def _model_asset_image3d(ctx: RunContext, hub: MCPHub, a: dict, spec: dict, pass_score: float) -> None:
    """图生 3D 建模（代替 Tripo）：目标裁剪图 → Hunyuan3D 多个种子出形体 → Blender 整理（朝向/尺寸/面数/投影取色成色块）
    → 多机位渲染 → 客观相似度挑最像的候选 → 独立评审员看图出差异台账 → 导出 Oskar 外观 GLB。"""
    crop = _target_image(ctx, a["id"])
    mask = Path(crop).with_name("mask.png")
    n = scene3d_limits(ctx.options)["candidates"]
    out_dir = ctx.dir / "shape3d" / a["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {"mode": "image3d", "candidates": [], "best": None}
    ctx.state["models"][a["id"]] = rec
    subject = f"{a['name']}：{a.get('description', '')}"
    # 图生 3D 需要「单个物体 + 干净背景」、分辨率够的图：场景裁剪带着天空/海/邻居，模型会把整块画面当成薄片；
    # 太小的裁剪（远处的小船、小蕨类）则什么表面都生成不出来。这两种情况都先按文章做法单独出一张资产图
    # （FLUX，单物体、纯灰底、四分之三视角）。用裁剪图全部失败时也改用资产图再试一次；仍失败就退回蓝图建模，不让整条流水线中断。
    from PIL import Image
    min_side = int(config.get("shape3d.min_crop_px", 256))
    crop_ok = mask.exists() and max(Image.open(crop).size) >= min_side
    want_flat = min(a["size_m"]) / max(max(a["size_m"]), 1e-6) < 0.12   # 目标本身就扁（如桥面、地毯）时不按薄片拒绝
    k = 0

    async def attempt(target: str, use_mask: bool) -> None:
        nonlocal k
        refine_args = {"size_m": a["size_m"], "budget": int(a.get("poly_budget") or 8000), "image": str(target),
                       "mask": str(mask) if use_mask else "", "colors": int(ctx.opt("shape3d_colors", 6)),
                       "name": a["id"]}
        pm = ((ctx.state.get("spec") or {}).get("palette_measured") or {}).get("assets", {}).get(a["id"])
        if pm:   # 配色环节的实测色：细高物件按色带上色，其余把投影色吸附到实测色块
            if pm.get("bands"):
                refine_args["bands"] = pm["bands"]
            else:
                # 框量出的物件色块里会混进背后的水面 / 天空：与实测水色、天色很近的色块不算这个物件的颜色
                spec_pm = (ctx.state.get("spec") or {}).get("palette_measured") or {}
                bg = [spec_pm[k][x] for k in ("water", "sky") if spec_pm.get(k) for x in ("base", "lit", "shade")]

                def near(h1, h2):
                    return sum((int(h1[i:i + 2], 16) - int(h2[i:i + 2], 16)) ** 2 for i in (1, 3, 5)) ** 0.5 < 40
                src = pm.get("clusters") or pm.get("swatches") or []
                sw = [x["hex"] for x in src if not any(near(x["hex"], b) for b in bg)]
                refine_args["palette"] = sw or [pm["base"]]
        tries = 0
        while len(rec["candidates"]) < n and tries < n * 2:
            tries += 1
            k += 1
            await ctx.control.checkpoint()
            ctx.emit("round", f"{a['name']} · 图生 3D 候选 {k}（目标 {n} 个有效）", {"round": k})
            raw = out_dir / f"cand{k}_raw.glb"
            try:
                gen = await vision.generate_shape(target, raw, seed=1000 * k + 7)
            except Exception as e:
                ctx.emit("warn", f"{a['name']} 候选 {k} 生成失败：{str(e)[:200]}")
                continue
            ref = await hub.call("blender_refine_generated", {"path": str(raw), **refine_args})
            if ref.get("ok") is False:
                ctx.emit("warn", f"{a['name']} 候选 {k} 整理失败：{str(ref.get('error'))[:200]}")
                continue
            size = ref.get("size") or [1, 1, 1]
            if not want_flat and min(size) / max(max(size), 1e-6) < 0.06:
                ctx.emit("warn", f"{a['name']} 候选 {k} 是一张薄片（尺寸 {size}），多半把背景也当成了物体，换种子重试")
                continue
            r = await hub.call("blender_render", {"views": ["front", "side", "persp"], "label": f"{a['id']}_i3d{k}"})
            renders = r.get("images", [])
            m = await vision.objective_similarity(target, renders) if renders else None
            cand = {"k": k, "raw": str(raw), "generate": gen, "target": str(target), "refine_args": refine_args,
                    "refine": {x: ref.get(x) for x in ("yaw", "mirror", "silhouette_iou", "faces_in", "faces_out", "size", "colors")},
                    "renders": renders, "metrics": m, "objective": vision.objective_index(m)}
            rec["candidates"].append(cand)
            for pth in renders:
                ctx.artifact("render", pth, f"{a['name']} 图生 3D 候选 {k}", asset=a["id"], round=k, target=target)
            ctx.emit("info", f"{a['name']} 候选 {k}：生成 {gen.get('seconds')} 秒（{gen.get('device')}），"
                     f"{ref.get('faces_in')}→{ref.get('faces_out')} 面，剪影 IoU {ref.get('silhouette_iou')}"
                     + (f"，DINO {m['dino']:.3f} / SigLIP {m['siglip']:.3f}" if m else ""), cand["refine"])
            ctx.save()

    if crop_ok:
        await attempt(crop, True)
    if not rec["candidates"]:
        if crop_ok:
            ctx.emit("info", f"{a['name']}：用参考裁剪图没有得到有效候选，改用单独生成的单物体资产图再试")
        iso = str(await _isolated_asset_image(ctx, a, spec, out_dir))
        rec["isolated"] = iso
        await attempt(iso, False)
    if not rec["candidates"]:
        ctx.emit("warn", f"{a['name']}：图生 3D 所有候选都失败（见 logs/shape3d.log），退回蓝图建模")
        a["method"] = "blueprint"
        await _model_asset_blueprint(ctx, hub, a, spec, scene3d_limits(ctx.options)["blueprint_rounds"], pass_score)
        return
    bi = max(range(len(rec["candidates"])), key=lambda i: (rec["candidates"][i]["objective"] or -1e9,
                                                           rec["candidates"][i]["refine"].get("silhouette_iou") or 0))
    best = rec["candidates"][bi]
    rec["best"] = bi
    obs = None
    try:  # 独立评审员（不参与生成）看图出差异台账；组装阶段的多机位评审会继续迭代摆放
        jev_dir = ctx.dir / "jev"
        jev_dir.mkdir(exist_ok=True)
        obs = await jev.observe(target=best["target"], renders=best["renders"], subject=subject,
                                compare_path=jev_dir / f"{a['id']}_i3d_compare.jpg",
                                facts=f"图生 3D 候选 {best['k']}，{best['refine'].get('faces_out')} 三角面，尺寸 {best['refine'].get('size')} 米",
                                style_context=style_agents.rules_text(ctx.options.get("style_profile"), "review"),
                                model_key=_reviewer(ctx), on_delta=ctx.delta_emitter())
        ctx.artifact("compare", obs["compare"], f"{a['name']} 图生 3D 对比", asset=a["id"], round=best["k"], score=obs["score"])
    except Exception as e:
        ctx.emit("warn", f"{a['name']}：评审员看图失败，只用客观相似度（{str(e)[:120]}）")
    await hub.call("blender_refine_generated", {"path": best["raw"], **best["refine_args"]})
    exp = await _export_asset(ctx, hub, a["id"])
    if not exp.get("ok"):
        raise RuntimeError(f"{a['id']} 导出失败: {exp.get('problems') or exp.get('error')}")
    score = obs["score"] if obs else round(best["objective"] or 0, 2)
    rec.update(glb=exp["path"], final_score=score, observation=obs)
    ctx.artifact("glb", exp["path"], f"{a['name']}.glb（图生 3D 候选 {best['k']}）", asset=a["id"], score=score,
                 triangles=exp.get("info", {}).get("triangles"), size=exp.get("info", {}).get("size"))
    ctx.emit("review", f"{a['name']}：选用候选 {best['k']}" + (f"，评审 {obs['score']:.1f}/10" if obs else ""),
             {"diffs": [d["observation"] for d in (obs or {}).get("diffs", [])]})
    ctx.save()


async def _model_asset(ctx: RunContext, hub: MCPHub, a: dict, spec: dict, max_rounds: int, pass_score: float):
    target = _target_image(ctx, a["id"])
    rec: dict[str, Any] = ctx.state["models"].get(a["id"]) or {"rounds": [], "best": None}
    ctx.state["models"][a["id"]] = rec
    size = "×".join(f"{x:g}" for x in a["size_m"])
    task = prompts.BLENDER_TASK.format(name=a["name"], id=a["id"], kind=a["kind"], role=a["role"],
                                       description=a["description"], size=size, budget=a["poly_budget"],
                                       style=spec.get("art_style", ""))
    feedback = None
    feedback_images: list[str] = []
    start_round = 1
    if rec["rounds"] and rec["best"] is not None:
        # 中断后续跑：从得分最高的一轮继续，沿用它的评审意见
        best = rec["rounds"][rec["best"]]
        await hub.call("blender_open_blend", {"path": best["blend"]})
        start_round = max(r["round"] for r in rec["rounds"]) + 1
        rv = best["review"]
        feedback = prompts.BLENDER_FEEDBACK.format(
            round=best["round"], score=f"{best['score']:.1f}", pass_score=pass_score,
            diffs="\n".join(f"- {d}" for d in rv["diffs"][:6]) or "- 无",
            actions="\n".join(f"{i + 1}. {x}" for i, x in enumerate(rv["next_actions"][:4])) or "1. 整体对照目标图调整",
            extra="")
        feedback_images = best["renders"]
        ctx.emit("info", f"{a['name']}：从第 {best['round']} 轮（{best['score']:.1f} 分）续跑")
    elif ctx.options.get("source_model"):
        task = await _load_source(ctx, hub, ctx.options["source_model"], task)
    for rnd in range(start_round, max_rounds + 1):
        ctx.emit("round", f"{a['name']} · 第 {rnd}/{max_rounds} 轮建模", {"asset": a["id"], "round": rnd})
        # 每轮全新上下文：场景状态在 Blender 里，模型只需看任务、目标图、当前渲染与评审意见。
        # 上下文保持在几千 token，解码速度与专注度都明显好于累积整段历史。
        if feedback is None:
            round_task, round_images = task, [target]
        else:
            round_task = (f"{task}\n\n{feedback}\n附图依次为：目标概念图、当前模型的 front/side/persp 渲染。"
                          "场景里已有上一轮的模型，不要 reset。")
            round_images = [target] + feedback_images
        result, _ = await run_agent(
            hub=hub, control=ctx.control, model_key=route(ctx, "blueprint"),
            system=prompts.BLENDER_SYSTEM + style_agents.rules_text(ctx.options.get("style_profile"), "model") + consultation.revision_rules(ctx.options), task=round_task,
            task_images=round_images, tool_prefixes=("blender_",),
            max_steps=int(ctx.opt("agent_max_steps", 24)), emit=ctx.emit)
        # 固定机位渲染 + 验收
        r = await hub.call("blender_render", {"views": ["front", "side", "persp"], "label": f"{a['id']}_r{rnd}"})
        if not r.get("ok"):
            ctx.emit("warn", f"验收渲染失败，重试一次：{str(r.get('error'))[:300]}", r)
            r = await hub.call("blender_render", {"views": ["front", "side", "persp"], "label": f"{a['id']}_r{rnd}b"})
        if not r.get("ok"):
            ctx.emit("error", f"验收渲染再次失败：{str(r.get('error'))[:300]}", r)
            feedback = f"流水线渲染失败：{r.get('error')}。请用 blender_scene_info 检查场景（是否有可见网格、异常尺寸），修复后 finish。"
            feedback_images = []
            continue
        renders = r["images"]
        info = await hub.call("blender_scene_info", {})
        facts = (f"三角面 {info.get('triangles')}（上限 {a['poly_budget']}），包围盒尺寸 {info.get('size')} 米"
                 f"（目标约 {a['size_m']}，Z 为高），对象数 {info.get('object_count')}")
        rev = await vision.review_against_target(
            target=[target], candidate=renders, subject=f"{a['name']}：{a['description']}",
            criteria=spec.get("acceptance", [])[:3] if ctx.run["mode"] == "asset" else [], facts=facts,
            style_context=style_agents.rules_text(ctx.options.get("style_profile"), "review") + consultation.revision_rules(ctx.options),
            pass_score=pass_score, model_key=route(ctx, "observe"), on_delta=ctx.delta_emitter())
        hard = _hard_checks(info, a)
        blend = await hub.call("blender_save_blend", {"name": f"{a['id']}_r{rnd:02d}"})
        round_rec = {"round": rnd, "score": rev["score"], "review": rev, "renders": renders, "hard": hard,
                     "blend": blend.get("path"), "agent": {"steps": result.steps, "calls": result.tool_calls,
                                                           "summary": result.summary, "seconds": round(result.seconds)}}
        rec["rounds"].append(round_rec)
        for p in renders:
            ctx.artifact("render", p, f"{a['name']} 第{rnd}轮", asset=a["id"], round=rnd, score=rev["score"],
                         target=target)
        ctx.emit("review", f"{a['name']} 第 {rnd} 轮：{rev['score']:.1f}/10 {'通过' if rev['pass'] else '未通过'}"
                 + (f"；硬性检查问题：{'; '.join(hard)}" if hard else ""), rev)
        if rec["best"] is None or rev["score"] > rec["rounds"][rec["best"]]["score"]:
            rec["best"] = len(rec["rounds"]) - 1
        ctx.save()
        if rev["pass"] and not hard:
            break
        # 回退保护：本轮明显退步时，从最佳轮次的 .blend 继续，而不是在更差的版本上叠加修改
        base = rec["rounds"][rec["best"]]
        if base is not round_rec and rev["score"] < base["score"] - 0.5 and base.get("blend"):
            await hub.call("blender_open_blend", {"path": base["blend"]})
            ctx.emit("info", f"第 {rnd} 轮退步（{rev['score']:.1f} < {base['score']:.1f}），下一轮从第 {base['round']} 轮继续")
            rev_for_fb, fb_renders, fb_hard = base["review"], base["renders"], base["hard"]
            regress_note = f"注意：你上一轮的修改使评分从 {base['score']:.1f} 降到 {rev['score']:.1f}，场景已恢复到较好的版本。换一种改法。\n"
        else:
            rev_for_fb, fb_renders, fb_hard, regress_note = rev, renders, hard, ""
        feedback = regress_note + prompts.BLENDER_FEEDBACK.format(
            round=rnd, score=f"{rev_for_fb['score']:.1f}", pass_score=pass_score,
            diffs="\n".join(f"- {d}" for d in rev_for_fb["diffs"][:6]) or "- 无",
            actions="\n".join(f"{i + 1}. {x}" for i, x in enumerate(rev_for_fb["next_actions"][:4]))
            or "1. 整体对照目标图调整",
            extra=(f"当前场景：{_scene_brief(info)}\n"
                   + (("硬性检查未通过：" + "；".join(fb_hard) + "\n") if fb_hard else "")))
        feedback_images = fb_renders
    # 选最佳轮次导出
    best = rec["rounds"][rec["best"]]
    if rec["best"] != len(rec["rounds"]) - 1 and best.get("blend"):
        await hub.call("blender_open_blend", {"path": best["blend"]})
        ctx.emit("info", f"{a['name']} 回退到得分最高的第 {best['round']} 轮（{best['score']:.1f}）导出")
    await hub.call("blender_run_python", {"code": "H.ground_to_origin()"})
    exp = await _export_asset(ctx, hub, a["id"])
    if not exp.get("ok"):
        raise RuntimeError(f"{a['id']} 导出失败: {exp.get('problems') or exp.get('error')}")
    rec["glb"] = exp["path"]
    rec["final_score"] = best["score"]
    ctx.artifact("glb", exp["path"], f"{a['name']}.glb", asset=a["id"], score=best["score"],
                 triangles=exp.get("info", {}).get("triangles"), size=exp.get("info", {}).get("size"))
    ctx.save()


BP_SHAPES = {"box", "rounded_box", "sphere", "capsule", "cylinder", "cone", "limb", "torus", "dome", "rock"}


def _apply_patch(bp: dict, patch: dict) -> tuple[dict, list[str]]:
    """把 {remove, update, add} 补丁确定性地应用到蓝图；返回新蓝图和问题列表。"""
    import copy
    parts = copy.deepcopy(bp.get("parts", []))
    names = {p["name"] for p in parts}
    problems = []
    rm = set(patch.get("remove") or [])
    for n in rm - names:
        problems.append(f"remove 了不存在的部件 {n}")
    parts = [p for p in parts if p["name"] not in rm]
    upd = patch.get("update") or {}
    by = {p["name"]: p for p in parts}
    for n, fields in upd.items():
        if n not in by:
            problems.append(f"update 了不存在的部件 {n}")
            continue
        if isinstance(fields, dict):
            by[n].update({k: v for k, v in fields.items() if k != "name"})
            if "at" in fields:
                by[n].pop("attach", None)  # 改成绝对位置
            if "attach" in fields:
                by[n].pop("at", None)
    for q in patch.get("add") or []:
        if isinstance(q, dict) and q.get("name"):
            if q["name"] in by:
                problems.append(f"add 的部件名 {q['name']} 已存在（改用 update）")
                continue
            parts.append(q)
            by[q["name"]] = q
    # attach 依赖的部件必须在前：按依赖做一次稳定排序
    ordered, placed = [], set()
    pending = list(parts)
    for _ in range(len(pending) + 1):
        rest = []
        for p in pending:
            dep = (p.get("attach") or {}).get("to") if isinstance(p.get("attach"), dict) else None
            if dep is None or dep in placed or dep not in by:
                ordered.append(p)
                placed.add(p["name"])
            else:
                rest.append(p)
        if not rest:
            break
        pending = rest
    else:
        ordered += pending
    return {**bp, "parts": ordered, "notes": patch.get("notes", bp.get("notes", ""))}, problems


ROLES = {
    "plan": "方案规划", "decompose": "场景拆分", "blueprint": "蓝图创建", "patch": "蓝图补丁",
    "observe": "视觉观察（差异台账）", "verify": "成对比较", "decide": "System One 复核决策", "godot": "Godot 编码",
}


def route(ctx: RunContext, role: str) -> str:
    """LLM 路由器：按子任务角色选模型。优先级：运行中手动切换的模型（控制台下拉框）> 运行选项 router.<角色>
    > config [router].<角色> > main。所选服务不存在时回退 main。每个角色首次路由时记一条事件，便于追溯。"""
    from .services import manager
    if ctx.control.model_key != "main":  # 用户在控制台手动切换了模型：所有角色都用它
        key = ctx.control.model_key
    else:
        prof = ctx.options.get("style_profile") or {}
        owner, _ = teams.stage_owner(prof, ctx.stage)   # 团队：本阶段负责人配置的模型优先；再看 Agent 配置的角色模型
        key = (ctx.options.get("router") or {}).get(role) or (owner or {}).get("model") \
            or ((prof.get("config") or {}).get("model") or {}).get(role) or config.get(f"router.{role}") or "main"
        if role in ("observe", "verify") and ctx.options.get("review_model"):
            key = ctx.options["review_model"]
    if f"llm-{key}" not in manager.services:
        key = "main"
    seen = ctx.state.setdefault("_routes", {})
    if seen.get(role) != key:
        seen[role] = key
        ctx.emit("route", f"路由：{ROLES.get(role, role)} → {manager.services[f'llm-{key}'].label}", {"role": role, "model": key})
    return key


def _reviewer(ctx: RunContext) -> str:
    return route(ctx, "observe")


HARD_FAIL_CAP = 4.0   # 有 1 项必需检查未过时的分数上限
HARD_FAIL_STEP = 1.0  # 每多 1 项再降 1 分

SHAPE_ALIASES = {"cube": "box", "cuboid": "box", "rectangle": "box", "prism": "box", "wedge": "box",
                 "rounded": "rounded_box", "roundedbox": "rounded_box", "ellipsoid": "sphere", "ball": "sphere",
                 "hemisphere": "dome", "halfsphere": "dome", "tube": "cylinder", "pipe": "cylinder",
                 "cyl": "cylinder", "pyramid": "cone", "cone_frustum": "cone", "frustum": "cone",
                 "ring": "torus", "donut": "torus", "boulder": "rock", "stone": "rock", "pill": "capsule"}


def _norm_part(p: Any) -> None:
    """容错：有的模型（如 Gemma）用 type/primitive 代替 shape，或写同义形状名；就地规整。"""
    if not isinstance(p, dict):
        return
    if not p.get("shape"):
        for k in ("type", "primitive", "geometry"):
            if isinstance(p.get(k), str):
                p["shape"] = p.pop(k)
                break
    if isinstance(p.get("shape"), str):
        sh = p["shape"].strip().lower().replace("-", "_").replace(" ", "_")
        p["shape"] = SHAPE_ALIASES.get(sh, SHAPE_ALIASES.get(sh.replace("_", ""), sh))


def _validate_blueprint(o: Any) -> str | None:
    if isinstance(o, dict):
        for p in o.get("parts") or []:
            _norm_part(p)
    if not isinstance(o, dict) or not isinstance(o.get("parts"), list) or not o["parts"]:
        return "需要 {\"parts\": [...]} 且非空"
    if len(o["parts"]) > 160:
        return "部件太多（最多 160；重复部件请用 array）"
    names = set()
    for i, p in enumerate(o["parts"]):
        if not isinstance(p, dict) or not p.get("name"):
            return f"第 {i} 个部件缺少 name"
        if p.get("shape") not in BP_SHAPES:
            return f"部件 {p['name']} 的 shape 必须是 {sorted(BP_SHAPES)} 之一"
        if p["name"] in names:
            return f"部件名重复: {p['name']}"
        at = p.get("attach")
        if at and at.get("to") not in names:
            return f"部件 {p['name']} attach 到 {at.get('to')!r}，但它不在前面已定义的部件里"
        if p["shape"] == "limb" and not (isinstance(p.get("from"), list) and isinstance(p.get("to"), list)):
            return f"limb 部件 {p['name']} 需要 from 和 to 坐标"
        names.add(p["name"])
    return None


async def _model_asset_blueprint(ctx: RunContext, hub: MCPHub, a: dict, spec: dict, max_rounds: int,
                                 pass_score: float) -> None:
    """蓝图建模：LLM 看图输出结构化部件清单 → 确定性构建 → 渲染评审 → LLM 修订蓝图，循环。"""
    target = _target_image(ctx, a["id"])
    rec: dict[str, Any] = ctx.state["models"].get(a["id"]) or {}
    if rec.get("rounds") and rec.get("mode") == "blueprint":
        if rec.get("champion") is None:
            rec["champion"] = _best_index(rec["rounds"], a.get("kind") in SYMMETRIC_KINDS)
        rec["best"] = _best_index(rec["rounds"], a.get("kind") in SYMMETRIC_KINDS)
    if rec.get("mode") != "blueprint":  # 从 agent 模式切换过来：旧轮次不可比，重新开始（旧渲染仍保留在产物中）
        if rec.get("rounds"):
            ctx.emit("info", f"{a['name']}：切换到蓝图建模，从头开始（agent 模式的 {len(rec['rounds'])} 轮记录已归档）")
            ctx.state.setdefault("models_archive", {}).setdefault(a["id"], []).append(rec)
        rec = {"rounds": [], "best": None, "mode": "blueprint"}
    ctx.state["models"][a["id"]] = rec
    size = "×".join(f"{x:g}" for x in a["size_m"])
    bp_dir = ctx.dir / "blueprints"
    bp_dir.mkdir(exist_ok=True)
    system = {"role": "system", "content": prompts.BLUEPRINT_SYSTEM + style_agents.rules_text(ctx.options.get("style_profile"), "model") + consultation.revision_rules(ctx.options)}
    last = rec["rounds"][rec["best"]] if rec["rounds"] else None
    blueprint = last.get("blueprint") if last else None  # agent 模式的旧轮次没有蓝图 → 重新拆解
    start = max((r["round"] for r in rec["rounds"]), default=0) + 1
    jev_dir = ctx.dir / "jev"
    jev_dir.mkdir(exist_ok=True)
    jev_history: list[dict] = rec.get("jev_history", [])
    if last is not None and not last.get("observation"):
        blueprint, last = None, None  # 旧版评审的轮次没有差异台账：重新拆解
    for r in rec["rounds"]:  # 续跑：给没有客观分的旧轮次补算，保证冠军/最佳轮在同一把尺子下比较
        if not r.get("metrics") and r.get("renders") and all(Path(x).exists() for x in r["renders"]):
            r["metrics"] = await vision.objective_similarity(target, r["renders"])
            r["objective"] = vision.objective_index(r["metrics"])
    for rnd in range(start, max_rounds + 1):
        await ctx.control.checkpoint()
        ctx.emit("round", f"{a['name']} · 第 {rnd}/{max_rounds} 轮（蓝图建模）", {"asset": a["id"], "round": rnd})
        t0 = time.time()
        if blueprint is None:
            msgs = [system, user_message(prompts.BLUEPRINT_CREATE.format(
                name=a["name"], kind=a["kind"], role=a["role"], description=a["description"], size=size,
                budget=a["poly_budget"], style=spec.get("art_style", "")), [target])]
        else:
            dec = last["decision"]
            by_id = {d["id"]: d for d in last["observation"]["diffs"]}
            focus_txt = "\n".join(f"- {i} [{by_id[i]['part']}]（{by_id[i]['category']}，严重度 {by_id[i]['severity']}）："
                                   f"{by_id[i]['observation']}" for i in dec["focus"] if i in by_id)
            others = "\n".join(f"- {d['id']} [{d['part']}] {d['observation']}" for d in last["observation"]["diffs"]
                                if d["id"] not in dec["focus"])[:1500]
            tried = "\n".join(f"- 第 {h['round']} 轮 {h['choice']}：{'；'.join(h['focus_text'])[:200]} → {h['result']}"
                               f"（{h['score_before']}→{h['score_after']}）" for h in jev_history[-5:])
            msgs = [system, user_message(prompts.BLUEPRINT_FOCUS.format(
                name=a["name"], size=size, round=last["round"], score=f"{last['score']:.1f}",
                choice=dec["next_step"], choice_desc=jev.CATEGORIES.get(dec["next_step"], "按严重度处理最大差异"),
                confidence=dec.get("confidence"), reason=dec.get("gate_reason", ""),
                focus=focus_txt or "- 无", plan=dec.get("plan") or "按焦点差异修改",
                others=others or "- 无", tried=tried or "- 无",
                blueprint=json.dumps(blueprint, ensure_ascii=False),
                report=json.dumps(last.get("build", {}).get("parts", {}), ensure_ascii=False)[:6000]),
                [last["observation"]["compare"]], max_side=1536)]
        ctx.emit("info", "LLM 正在" + ("拆解概念图生成部件蓝图…" if blueprint is None else "根据 System One 焦点生成蓝图补丁…"))
        if blueprint is None:
            new_bp = await LLM(route(ctx, "blueprint")).json(msgs, on_delta=ctx.delta_emitter(), thinking_budget=6144,
                                                             validate=_validate_blueprint, retries=2)
        else:
            base_bp = blueprint

            def check_patch(o: Any) -> str | None:
                # 宽松：引用不存在部件的操作会被丢弃（记警告），只要还有有效改动且结果蓝图合法即可
                if not isinstance(o, dict) or not any(o.get(k) for k in ("remove", "update", "add")):
                    return "需要非空补丁 JSON：{\"remove\": [...], \"update\": {...}, \"add\": [...]}"
                for p in o.get("add") or []:
                    _norm_part(p)
                if isinstance(o.get("update"), dict):
                    for p in o["update"].values():
                        _norm_part(p)
                cand, _ = _apply_patch(base_bp, o)
                if cand["parts"] == base_bp["parts"]:
                    return "补丁没有产生任何有效改动（部件名要与当前蓝图完全一致）"
                return _validate_blueprint(cand)

            try:
                patch = await LLM(route(ctx, "patch")).json(msgs, on_delta=ctx.delta_emitter(), thinking_budget=6144,
                                                            validate=check_patch, retries=2)
            except Exception as e:  # 补丁失败不让阶段失败：本轮跳过，下一轮重新决策
                ctx.emit("warn", f"第 {rnd} 轮补丁生成失败，跳过本轮：{str(e)[:200]}")
                jev_history.append({"round": rnd, "choice": last["decision"]["next_step"], "focus_text": [],
                                    "score_before": last["score"], "score_after": last["score"],
                                    "result": "补丁无效"})
                continue
            new_bp, probs = _apply_patch(base_bp, patch)
            ctx.emit("info", f"补丁：删除 {len(patch.get('remove') or [])}、修改 {len(patch.get('update') or {})}、"
                     f"新增 {len(patch.get('add') or [])} 个部件 — {patch.get('notes', '')}"
                     + (f"（忽略：{'；'.join(probs[:3])}）" if probs else ""), patch)
        # 构建；有构建错误时让模型修正一次
        build = await hub.call("blender_build_blueprint", {"blueprint": new_bp})
        if build.get("ok") and build.get("errors"):
            ctx.emit("warn", f"蓝图构建有 {len(build['errors'])} 个错误，请模型修正", build["errors"])
            fix_msgs = msgs + [{"role": "assistant", "content": json.dumps(new_bp, ensure_ascii=False)},
                               {"role": "user", "content": "构建报错：\n" + "\n".join(build["errors"][:10]) +
                                "\n请修正后输出完整蓝图 JSON。"}]
            try:
                fixed = await LLM(route(ctx, "blueprint")).json(fix_msgs, thinking_budget=2048,
                                                                validate=_validate_blueprint, retries=1)
                build2 = await hub.call("blender_build_blueprint", {"blueprint": fixed})
                if build2.get("ok") and len(build2.get("errors", [])) < len(build["errors"]):
                    new_bp, build = fixed, build2
            except Exception as e:  # 修正失败就用原蓝图继续评审
                ctx.emit("warn", f"蓝图修正失败：{e}")
        if not build.get("ok"):
            raise RuntimeError(f"蓝图构建失败：{build.get('error')}")
        blueprint = new_bp
        bp_path = bp_dir / f"{a['id']}_r{rnd:02d}.json"
        bp_path.write_text(json.dumps(new_bp, ensure_ascii=False, indent=1))
        views = ["persp_left", "front", "side", "persp"]  # 第一张与概念图常见视角一致，便于逐项对照
        r = await hub.call("blender_render", {"views": views, "label": f"{a['id']}_r{rnd}"})
        if not r.get("ok"):
            r = await hub.call("blender_render", {"views": views, "label": f"{a['id']}_r{rnd}b"})
        if not r.get("ok"):
            raise RuntimeError(f"渲染失败：{r.get('error')}")
        renders = r["images"]
        info = await hub.call("blender_scene_info", {})
        hard = _hard_checks(info, a)
        if build.get("floating_major", build.get("floating")):
            hard.append(f"悬空部件 {build.get('floating_major', build.get('floating'))[:8]}")
        facts = (f"{build.get('built')} 个部件，三角面 {info.get('triangles')}（上限 {a['poly_budget']}），"
                 f"总尺寸 {info.get('size')} 米（目标约 {a['size_m']}），悬空部件 {build.get('floating') or '无'}")
        sym_pre = _symmetry(build.get("parts") or {})
        if a.get("kind") in SYMMETRIC_KINDS and sym_pre is not None and sym_pre < 0.75:
            facts += f"；【确定性检查】左右不对称：模型向一侧伸出的长度只有另一侧的 {sym_pre:.0%}（可能缺少一侧的翼/手臂/腿）"
        # ── JEV System One：观察一次 → 台账评分 → 类型化决策 → 本地规则 ──
        subject = f"{a['name']}：{a['description']}"  # 决策用（需要知道资产意图）
        # 观察只给名称不给描述：目标图对不上时，评审不能按文字描述“脑补”打高分（实测生态舱裁错仍得 8.0）
        obs = await jev.observe(target=target, renders=renders, subject=a["name"],
                                compare_path=jev_dir / f"{a['id']}_r{rnd:02d}_compare.jpg", facts=facts,
                                style_context=style_agents.rules_text(ctx.options.get("style_profile"), "review") + consultation.revision_rules(ctx.options),
                                model_key=_reviewer(ctx), on_delta=ctx.delta_emitter())
        if not obs.get("target_visible", True):
            ctx.emit("warn", f"{a['name']} 第 {rnd} 轮：评审认为目标图里看不到该对象（可能裁剪错误），本轮不判通过")
        checks = [
            # 小悬空件已由构建器自动落地；只有体量显著（>1% 总体积）的悬空才是必需检查失败
            {"name": "无显著悬空部件", "required": True,
             "passed": not build.get("floating_major", build.get("floating")), "category": "position"},
            {"name": "三角面不超预算", "required": True,
             "passed": (info.get("triangles") or 0) <= a["poly_budget"] * 1.2, "category": "detail"},
            {"name": "尺寸量级与目标一致", "required": True,
             "passed": not any("量级" in h for h in hard), "category": "proportion"},
        ]
        sym = _symmetry(build.get("parts") or {})
        if a.get("kind") in SYMMETRIC_KINDS and sym is not None:
            checks.append({"name": f"左右对称（X 向两侧伸出比 {sym:.2f}）", "required": True, "passed": sym >= 0.75,
                           "category": "missing"})

        failed_req = [c["name"] for c in checks if c.get("required") and not c["passed"]]
        if failed_req:  # 必需检查未过：台账分封顶，避免“悬空/尺寸错”的模型因评审手松拿到高分并成为冠军
            cap = max(1.0, HARD_FAIL_CAP - HARD_FAIL_STEP * (len(failed_req) - 1))
            if obs["score"] > cap:
                ctx.emit("info", f"必需检查未通过（{'、'.join(failed_req)}）：台账分 {obs['score']:.1f} 封顶为 {cap:.1f}")
                obs["raw_score"], obs["score"] = obs["score"], cap
        objm = await vision.objective_similarity(target, renders)
        obj = vision.objective_index(objm)
        passed = jev.ledger_pass(obs["diffs"], pass_score) and all(c["passed"] for c in checks) \
            and obs.get("target_visible", True)
        min_dino = float(config.get("metrics.pass_min_dino", 0.25))
        if passed and objm is not None and objm["dino"] < min_dino:  # 台账说通过、客观结构相似度却很低 → 不算通过
            passed = False
            ctx.emit("info", f"{a['name']} 第 {rnd} 轮台账达到通过线，但客观结构相似度 DINO {objm['dino']:.3f} "
                             f"低于 {min_dino}，不判通过")
        verdict = None
        if last is not None and last.get("renders"):
            prev_focus = [d for d in last["observation"]["diffs"] if d["id"] in (last.get("decision") or {}).get("focus", [])]
            verdict = await jev.verify_pair(target=target, prev_render=last["renders"][0], cur_render=renders[0],
                                            focus=prev_focus, out=jev_dir / f"{a['id']}_r{rnd:02d}_pair.jpg",
                                            model_key=route(ctx, "verify"), on_delta=ctx.delta_emitter())
        rev = {"score": obs["score"], "pass": passed, "matches": obs["matches"],
               "diffs": [f"[{d['id']} {d['part']}·{d['category']}·S{d['severity']}] {d['observation']}" for d in obs["diffs"]],
               "next_actions": []}
        # 上一轮的焦点是否被解决：记入历史（System One 下一次决策会避开无效改法）
        if last is not None and last.get("decision"):
            prev_focus = [d for d in last["observation"]["diffs"] if d["id"] in last["decision"]["focus"]]
            jev_history.append({"round": rnd, "choice": last["decision"]["next_step"],
                                "focus_text": [f"{d['part']}：{d['observation']}"[:120] for d in prev_focus],
                                "score_before": last["score"], "score_after": obs["score"],
                                "result": _improved(obj, vision.objective_index(last.get("metrics")), verdict,
                                                    obs["score"], last["score"]),
                                "pairwise": {k: verdict.get(k) for k in ("closer", "focus", "new_problems")} if verdict else None})
            _log_system_one(ctx, a, last, obs)
        decision = None
        if not passed:
            decision = await jev.decide(subject=subject, observation=obs, checks=checks, history=jev_history,
                                        model_key=route(ctx, "decide"), on_delta=ctx.delta_emitter())
            rev["next_actions"] = [f"[{decision['next_step']}] {decision.get('plan', '')}"]
        (jev_dir / f"{a['id']}_r{rnd:02d}_evidence.json").write_text(json.dumps(
            {"observation": obs, "checks": checks, "history": jev_history[-6:]}, ensure_ascii=False, indent=1))
        if decision:
            (jev_dir / f"{a['id']}_r{rnd:02d}_decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=1))
        blend = await hub.call("blender_save_blend", {"name": f"{a['id']}_r{rnd:02d}"})
        round_rec = {"round": rnd, "score": rev["score"], "review": rev, "renders": renders, "hard": hard,
                     "observation": obs, "decision": decision, "checks": checks, "verdict": verdict,
                     "metrics": objm, "objective": obj,
                     "blend": blend.get("path"), "blueprint": new_bp, "blueprint_path": str(bp_path),
                     "build": {k: build.get(k) for k in ("built", "errors", "floating", "floating_major", "auto_grounded",
                                                          "parts", "size", "triangles")},
                     "agent": {"steps": 1, "calls": 1, "summary": new_bp.get("notes", ""),
                               "seconds": round(time.time() - t0)}}
        rec["rounds"].append(round_rec)
        rec["jev_history"] = jev_history
        ctx.artifact("compare", obs["compare"], f"{a['name']} 第{rnd}轮 对比", asset=a["id"], round=rnd,
                     score=rev["score"])
        for p in renders:
            ctx.artifact("render", p, f"{a['name']} 第{rnd}轮", asset=a["id"], round=rnd, score=rev["score"],
                         target=target)
        sev = {k: sum(1 for d in obs["diffs"] if d["severity"] == k) for k in (3, 2, 1)}
        ctx.emit("review", f"{a['name']} 第 {rnd} 轮：{rev['score']:.1f}/10 {'通过' if passed else '未通过'}"
                 f"（差异 严重{sev[3]}/明显{sev[2]}/细节{sev[1]}，{build.get('built')} 部件，{round(time.time() - t0)}s）"
                 + (f"；客观相似度 DINO {objm['dino']:.3f} / SigLIP {objm['siglip']:.3f}" if objm else "")
                 + (f"；成对比较：{'本轮更接近目标' if jev.accept_b(verdict) else '未比上一版更好'}（{verdict.get('reason', '')[:60]}）"
                    if verdict else "")
                 + (f"；下一步 {decision['next_step']}（置信度 {decision.get('confidence')}，{decision['gate_reason']}）"
                    if decision else ""), {**rev, "decision": decision})
        # 冠军制（爬山法）：成对比较认定本轮更接近目标才接替冠军；没有成对比较时按台账分数
        cur_ok = all(c["passed"] for c in checks if c.get("required"))
        champ_i = rec.get("champion")
        champ = rec["rounds"][champ_i] if champ_i is not None and champ_i < len(rec["rounds"]) - 1 else None
        champ_ok = all(c.get("passed") for c in champ.get("checks", []) if c.get("required")) if champ else False
        champ_obj = vision.objective_index(champ.get("metrics")) if champ else None
        if champ is None:
            won = True
        elif obj is not None and champ_obj is not None and abs(obj - champ_obj) > OBJ_TIE:
            won = obj > champ_obj and (cur_ok or not champ_ok)
        elif verdict is not None:
            won = _accept_round(verdict, rev["score"], champ["score"]) and (cur_ok or not champ_ok)
        else:
            won = rev["score"] >= champ["score"] and (cur_ok or not champ_ok)
        round_rec["won"] = won
        if won:
            rec["champion"] = len(rec["rounds"]) - 1
        # 最终/展示用“最佳”= 通过必需检查中台账分最高的一轮；冠军只是下一轮修改的基础
        rec["best"] = _best_index(rec["rounds"], a.get("kind") in SYMMETRIC_KINDS)
        rec["forecast"] = _forecast(rec, pass_score)
        round_rec["forecast"] = rec["forecast"]
        fc = rec["forecast"]
        ctx.emit("forecast", f"{a['name']}：冠军 {fc['champion_score']:.1f} 分，距通过线差 {fc['gap']:.1f}；"
                 + (f"按最近 {fc['window']} 轮速度（每轮 +{fc['rate']:.2f}）预计还需约 {fc['eta_rounds']} 轮"
                    if fc["eta_rounds"] else f"最近 {fc['stalled']} 轮冠军没有进步，处于停滞"), fc)
        ctx.save()
        patience = scene3d_limits(ctx.options)["patience"] if ctx.run.get("mode") == "scene3d" \
            else int(ctx.opt("plateau_patience", 6))
        if not passed and fc["stalled"] >= patience:
            ctx.emit("info", f"{a['name']}：连续 {fc['stalled']} 轮没有胜过冠军，提前结束（plateau_patience={patience}）")
            break
        if passed:
            rec["champion"] = rec["best"] = len(rec["rounds"]) - 1
            ctx.save()
            break
        best = rec["rounds"][rec["champion"]]
        if not won:
            ctx.emit("info", f"第 {rnd} 轮未胜过冠军第 {best['round']} 轮（台账 {rev['score']:.1f} vs {best['score']:.1f}"
                     + (f"，客观指数 {obj:.1f} vs {vision.objective_index(best.get('metrics')):.1f}"
                        if obj is not None and best.get("metrics") else "") + "），"
                     "回滚到冠军蓝图，System One 已记录这次无效改法")
            # 回滚后按冠军的台账 + 含失败尝试的历史重新决策（System One 会避开刚失败的改法）
            try:
                best["decision"] = await jev.decide(
                    subject=subject, observation=best["observation"], checks=best.get("checks", checks),
                    history=jev_history, model_key=route(ctx, "decide"), on_delta=ctx.delta_emitter())
                ctx.emit("info", f"回滚后重新决策：{best['decision']['next_step']}，焦点 {best['decision']['focus']}"
                         f"（{best['decision'].get('gate_reason', '')[:80]}）")
            except Exception as e:  # 决策失败则沿用本轮的新决策
                ctx.emit("warn", f"回滚后重新决策失败：{str(e)[:120]}")
                best["decision"] = best.get("decision") or decision
            blueprint, last = best["blueprint"], best
        else:
            last = round_rec
    # 导出最佳轮（蓝图确定性重建）
    rec["best"] = _best_index(rec["rounds"], a.get("kind") in SYMMETRIC_KINDS)
    best = rec["rounds"][rec["best"]]
    rec["exported_round"] = best["round"]
    await hub.call("blender_build_blueprint", {"blueprint": best["blueprint"]})
    await hub.call("blender_run_python", {"code": "H.ground_to_origin()"})
    exp = await _export_asset(ctx, hub, a["id"])
    if not exp.get("ok"):
        raise RuntimeError(f"{a['id']} 导出失败: {exp.get('problems') or exp.get('error')}")
    rec["glb"] = exp["path"]
    rec["final_score"] = best["score"]
    ctx.artifact("glb", exp["path"], f"{a['name']}.glb", asset=a["id"], score=best["score"],
                 triangles=exp.get("info", {}).get("triangles"), size=exp.get("info", {}).get("size"))
    ctx.save()


async def _load_source(ctx: RunContext, hub: MCPHub, source: str, task: str) -> str:
    """把已有资产复制到运行目录后载入 Blender；原文件绝不修改。"""
    src = Path(source).expanduser()
    if not src.exists():
        raise RuntimeError(f"源模型不存在: {src}")
    dst_dir = ctx.dir / "source"
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / src.name
    if not dst.exists():
        shutil.copy2(src, dst)
    if dst.suffix == ".blend":
        res = await hub.call("blender_open_blend", {"path": str(dst)})
    else:
        await hub.call("blender_reset_scene", {})
        res = await hub.call("blender_import_model", {"path": str(dst)})
    if not res.get("ok", True):
        raise RuntimeError(f"载入源模型失败: {res.get('error')}")
    r = await hub.call("blender_render", {"views": ["front", "side", "persp"], "label": "source"})
    for p in r.get("images", []):
        ctx.artifact("render", p, "源模型（修改前）", round=0)
    ctx.emit("info", f"已载入源模型副本 {dst.name}：{res.get('object_count')} 个对象，{res.get('triangles')} 三角面")
    return (task.replace("先 blender_reset_scene，然后开始建模。", "")
            + f"\n\n【重要】场景中已载入现有资产的副本（{src.name}，{res.get('object_count')} 个对象）。"
              "不要重置场景；在它的基础上修改，使其接近目标图。保留原有对象层级与命名，只调整造型、比例、材质或增补部件。"
              "先 blender_scene_info 了解结构，再 blender_render 对比目标图。")


SYMMETRIC_KINDS = {"vehicle", "aircraft", "character", "creature", "animal", "robot"}


def _symmetry(parts: dict) -> float | None:
    """以最大部件（机身/躯干）中心为轴，比较模型向 -X 与 +X 两侧伸出的长度；1=完全对称。"""
    if len(parts) < 2:
        return None
    main = max(parts.values(), key=lambda p: p["size"][0] * p["size"][1] * p["size"][2])
    cx = main["center"][0]
    left = max(cx - (p["center"][0] - p["size"][0] / 2) for p in parts.values())
    right = max((p["center"][0] + p["size"][0] / 2) - cx for p in parts.values())
    if max(left, right) <= 1e-6:
        return None
    return round(min(left, right) / max(left, right), 3)


OBJ_TIE = 0.5  # 客观指数差在 ±0.5 个波动单位内视为持平，交给成对比较/台账


def _improved(obj: float | None, prev_obj: float | None, verdict: dict | None, score: float, prev_score: float) -> str:
    """本轮相对上一轮是否改善：有客观分且差距明显时以客观分为准。"""
    if obj is not None and prev_obj is not None and abs(obj - prev_obj) > OBJ_TIE:
        return "改善" if obj > prev_obj else "未改善"
    if verdict:
        return "改善" if jev.accept_b(verdict) else "未改善"
    return "改善" if score > prev_score else "未改善"


def _accept_round(verdict: dict, new_score: float, champ_score: float) -> bool:
    """接替冠军的条件：成对比较认为更接近目标且台账分没有明显下降；或成对比较持平但台账分明显更高。
    只看成对比较会因为每次“略好一点”的宽松判断而一路漂移变差（实测主殿从 6.2 漂到 3.2）。"""
    if jev.accept_b(verdict) and new_score >= champ_score - 0.5:
        return True
    return verdict.get("closer") != "A" and new_score >= champ_score + 1.0


def _forecast(rec: dict, pass_score: float, window: int = 5) -> dict:
    """收敛预测：冠军分数随轮次的变化 → 估计还需几轮达到通过线；停滞轮数用于提前结束。"""
    rounds = rec["rounds"]
    champ_hist, cur = [], None
    for i, r in enumerate(rounds):  # 每一轮结束时的冠军分数
        won = r.get("won")
        if won is None:  # 旧记录没有 won 字段：首轮或成对比较胜出视为夺冠
            won = i == 0 or bool(r.get("verdict") and jev.accept_b(r["verdict"]))
        if won or cur is None:
            cur = r["score"]
        champ_hist.append(cur)
    champ_score = rounds[rec["champion"]]["score"]
    stalled = len(rounds) - 1 - rec["champion"]
    k = min(window, len(champ_hist) - 1)
    rate = (champ_hist[-1] - champ_hist[-1 - k]) / k if k > 0 else 0.0
    gap = max(0.0, pass_score - champ_score)
    eta = None if gap == 0 else (math.ceil(gap / rate) if rate > 0.05 else None)
    return {"champion_score": champ_score, "gap": round(gap, 2), "rate": round(rate, 3), "window": k,
            "eta_rounds": eta, "stalled": stalled, "open_severe": sum(
                1 for d in rounds[rec["champion"]]["observation"]["diffs"] if d["severity"] >= 2)}


def _best_index(rounds: list[dict], symmetric: bool = False) -> int:
    """最佳轮：先要求必需检查全部通过，再比台账分数（不能用缺翼但高分的轮次当最佳）。
    对称类资产会对历史轮次现场补算对称性（旧记录里可能没有这项检查）。"""
    def key(i: int):
        r = rounds[i]
        ok = all(c.get("passed") for c in r.get("checks", []) if c.get("required"))
        if symmetric:
            sym = _symmetry((r.get("build") or {}).get("parts") or {})
            ok = ok and (sym is None or sym >= 0.75)
        return (ok, r["score"], r["round"])
    oks = [i for i in range(len(rounds)) if key(i)[0]] or list(range(len(rounds)))
    if all(rounds[i].get("metrics") for i in oks):  # 客观分可用：同一资产内以客观相似度选最佳
        return max(oks, key=lambda i: (vision.objective_index(rounds[i]["metrics"]), rounds[i]["score"]))
    return max(range(len(rounds)), key=key)


def _log_system_one(ctx: RunContext, a: dict, prev: dict, obs: dict) -> None:
    """积累 System One 训练数据：决策证据 + 最终选择 + 执行后的结果（用于日后微调 Laya）。"""
    dec = prev.get("decision") or {}
    if not dec.get("evidence"):
        return
    row = {"run": ctx.id, "asset": a["id"], "round": prev["round"], "evidence": dec["evidence"],
           "choice": dec.get("next_step"), "decider": dec.get("decider"), "focus": dec.get("focus"),
           "laya": ((dec.get("system_one") or {}).get("answers") or {}).get("next_step"),
           "score_before": prev["score"], "score_after": obs["score"],
           "outcome": "improved" if obs["score"] > prev["score"] else ("same" if obs["score"] == prev["score"] else "regressed")}
    path = config.ROOT / "data" / "system_one_dataset.jsonl"
    path.parent.mkdir(exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _scene_brief(info: dict) -> str:
    objs = info.get("objects", [])
    parts = [f"{o['name']}({'×'.join(f'{d:g}' for d in o.get('dimensions', []))}m"
             + (f",{','.join(o['materials'])}" if o.get("materials") else "") + ")" for o in objs[:30]]
    return (f"{info.get('object_count')} 个对象，{info.get('triangles')} 三角面，总尺寸 {info.get('size')}；"
            + "；".join(parts))


def _hard_checks(info: dict, a: dict) -> list[str]:
    issues = []
    if not info.get("ok", True) or not info.get("triangles"):
        issues.append("场景中没有网格")
        return issues
    if info["triangles"] > a["poly_budget"] * 1.2:
        issues.append(f"三角面 {info['triangles']} 超过预算 {a['poly_budget']}")
    if info.get("nonfinite_transforms"):
        issues.append(f"存在非有限变换 {info['nonfinite_transforms']}")
    # 朝向有歧义（如信封平放/立放），按排序后的三个尺寸比较量级
    size = sorted(info.get("size") or [0, 0, 0])
    tgt = sorted(a["size_m"])
    for s, t in zip(size, tgt):
        if t > 0 and s > 0 and (s / t > 3 or t / s > 3):
            issues.append(f"尺寸 {info.get('size')} 与目标 {a['size_m']} 量级相差超过 3 倍")
            break
    return issues


async def _export_asset(ctx: RunContext, hub: MCPHub, asset_id: str) -> dict:
    """导出单个资产 GLB；默认先加 Oskar 统一外观（平面着色 + 描边），与 Oskar 场景风格一致（选项 oskar_look=false 关闭）。"""
    if ctx.opt("oskar_look", True):
        look = await hub.call("blender_oskar_look", {})
        ctx.emit("info", f"{asset_id}：已加 Oskar 统一外观（描边 {look.get('outline')} 米，{look.get('outlined')} 个部件）")
    return await hub.call("blender_export_glb", {"asset_name": asset_id})


def _godot_project(ctx: RunContext) -> Path:
    return ctx.dir / "godot_project"


async def stage_assemble(ctx: RunContext) -> None:
    spec = ctx.state["spec"]
    if ctx.run["mode"] == "assembly":
        assembly.validate(ctx.options, ctx.dir)
        ctx.state.setdefault("assembly", {})["journey_approved"] = False
        ctx.state.pop("final_review", None)
        ctx.save()
    max_rounds = int(ctx.opt("godot_max_rounds", 6))
    pass_score = float(ctx.opt("model_pass_score", 7.5))
    key_art = _target_image(ctx, "_key_art") if "_key_art" in ctx.state.get("concepts", {}) else None
    rec = ctx.state.setdefault("assemble", {"rounds": []})
    world = ctx.state.get("world")
    async with MCPHub(ctx.dir, godot_project=_godot_project(ctx), servers=["godot"]) as hub:
        imp = await hub.call("godot_import_assets", {})
        if not imp.get("ok"):
            ctx.emit("warn", "资产导入有错误", imp)
        world_txt = ""
        if world:   # Oskar 世界：写入世界配置；main.gd 从继承世界脚本的模板开始
            from .godot import oskar_scene as OG
            cfg = OG.scene_config("oskar_world.glb", walk=world.get("walk"), paths=world.get("paths"), game=True)
            (_godot_project(ctx) / "oskar_scene.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1))
            mg = _godot_project(ctx) / "main.gd"
            tpl = (config.ROOT / "templates" / "godot_project" / "main.gd").read_text()
            if not mg.exists() or mg.read_text() == tpl:
                mg.write_text(OG.MAIN_GD_GAME)
            ends = [p[-1] for p in cfg["paths"] if p]
            world_txt = (f"\n\nOskar 世界：可站立点 {len(cfg['walk'])} 个，出生点 {cfg['spawn']}，"
                         f"通路 {len(cfg['paths'])} 条（终点 {ends[:3]}）。")
        assets_txt = json.dumps({k: v for k, v in imp.get("assets", {}).items() if "oskar_world" not in k}, ensure_ascii=False)
        roles = "\n".join(f"- res://assets/{a['id']}.glb：{a['name']}（{a['role']}）" for a in spec["assets"])
        spec_txt = json.dumps({k: spec.get(k) for k in ("title", "camera", "controls", "core_loop", "scene",
                                                        "gameplay", "acceptance")}, ensure_ascii=False, indent=1)
        task = prompts.GODOT_TASK.format(spec=spec_txt, assets=roles + "\n尺寸：" + assets_txt) + world_txt
        godot_system = prompts.GODOT_SYSTEM + (prompts.GODOT_OSKAR_WORLD if world else "") + style_agents.rules_text(ctx.options.get("style_profile"), "assemble") + consultation.revision_rules(ctx.options)
        feedback = None
        shots: list[str] = []
        start = max((r["round"] for r in rec["rounds"]), default=0) + 1
        main_gd = _godot_project(ctx) / "main.gd"
        template_gd = config.ROOT / "templates" / "godot_project" / "main.gd"
        fresh = {template_gd.read_text()}
        if world:
            from .godot import oskar_scene as OG
            fresh.add(OG.MAIN_GD_GAME)
        if main_gd.exists() and main_gd.read_text() not in fresh:
            # 续跑：工程里已有代码，先试玩评审一次，把意见交给 agent 接着改，而不是从头重写
            test = await _playtest(ctx, hub, f"resume{start}")
            rev = await _review_play(ctx, spec, key_art, test, pass_score)
            ctx.emit("review", f"续跑前试玩：{rev['score']:.1f}/10", {**rev, "facts": test["facts"]})
            if rev["pass"] and test["hard_ok"]:
                rec["rounds"].append({"round": start, "score": rev["score"], "review": rev, "test": test,
                                      "agent": {"steps": 0, "summary": "续跑前已通过"}})
                ctx.save()
                return
            feedback = prompts.GODOT_FEEDBACK.format(
                round=start - 1, score=f"{rev['score']:.1f}", pass_score=pass_score, facts=test["facts"],
                diffs="\n".join(f"- {d}" for d in rev["diffs"][:6]) or "- 无",
                actions="\n".join(f"{i + 1}. {x}" for i, x in enumerate(rev["next_actions"][:4])))
            shots = test["images"][-3:]
        # 结束时保留得分最高一轮的代码（而不是最后一轮）
        if rec["rounds"]:
            best = max(rec["rounds"], key=lambda r: r["score"])
            last_round = rec["rounds"][-1]
            if best is not last_round and best.get("snapshot"):
                _restore_project(ctx, best["snapshot"])
                ctx.emit("info", f"组装结束：恢复得分最高的第 {best['round']} 轮代码（{best['score']:.1f}）")
        for rnd in range(start, max_rounds + 1):
            ctx.emit("round", f"Godot 组装 · 第 {rnd}/{max_rounds} 轮", {"round": rnd})
            # 每轮全新上下文：代码在工程里（可用 godot_read_file 查看），只带任务、评审意见与最新截图
            round_task = task if feedback is None else (
                f"{task}\n\n{feedback}\n工程里已有上一轮的代码，先 godot_project_files / godot_read_file 查看再修改。"
                "附图依次为：关键画面目标、上一轮试玩截图。")
            round_images = ([key_art] if key_art else []) + (shots if feedback is not None else [])
            result, _ = await run_agent(
                hub=hub, control=ctx.control, model_key=route(ctx, "godot"), system=godot_system, task=round_task,
                task_images=round_images or None, tool_prefixes=("godot_",),
                max_steps=int(ctx.opt("agent_max_steps", 24)), emit=ctx.emit)
            test = await _playtest(ctx, hub, f"r{rnd}")
            rev = await _review_play(ctx, spec, key_art, test, pass_score)
            snap = _snapshot_project(ctx, rnd)
            rec["rounds"].append({"round": rnd, "score": rev["score"], "review": rev, "test": test, "snapshot": snap,
                                  "agent": {"steps": result.steps, "summary": result.summary}})
            best_i = max(range(len(rec["rounds"])), key=lambda i: rec["rounds"][i]["score"])
            rec["best"] = best_i
            ctx.save()
            best = rec["rounds"][best_i]
            if best["round"] != rnd and rev["score"] < best["score"] - 0.5 and best.get("snapshot"):
                # 回退保护：本轮改坏了（例如脚本解析失败），恢复最佳轮的脚本再继续
                _restore_project(ctx, best["snapshot"])
                ctx.emit("info", f"试玩第 {rnd} 轮退步（{rev['score']:.1f} < {best['score']:.1f}），已恢复第 {best['round']} 轮的代码")
                rev = {**best["review"], "diffs": [f"（上一轮修改导致评分降到 {rev['score']:.1f}，问题：{'；'.join(rev['diffs'][:2])}，已回滚）"] + best["review"].get("diffs", [])}
                test = best["test"]
            ctx.emit("review", f"试玩第 {rnd} 轮：{rev['score']:.1f}/10 {'通过' if rev['pass'] else '未通过'}", rev)
            if rev["pass"] and test["hard_ok"]:
                break
            feedback = prompts.GODOT_FEEDBACK.format(
                round=rnd, score=f"{rev['score']:.1f}", pass_score=pass_score, facts=test["facts"],
                diffs="\n".join(f"- {d}" for d in rev["diffs"][:6]) or "- 无",
                actions="\n".join(f"{i + 1}. {x}" for i, x in enumerate(rev["next_actions"][:4])))
            shots = test["images"][-3:]


def _snapshot_project(ctx: RunContext, rnd: int) -> str:
    proj = _godot_project(ctx)
    dst = ctx.dir / "godot_snapshots" / f"r{rnd:02d}"
    if dst.exists():
        shutil.rmtree(dst)
    for f in proj.rglob("*"):
        rel = f.relative_to(proj)
        if f.is_file() and f.suffix in (".gd", ".tscn", ".tres", ".gdshader") and not rel.parts[0].startswith("."):
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst / rel)
    return str(dst)


def _restore_project(ctx: RunContext, snap: str) -> None:
    proj = _godot_project(ctx)
    src = Path(snap)
    for f in src.rglob("*"):
        if f.is_file():
            (proj / f.relative_to(src)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, proj / f.relative_to(src))


PLAYTEST_INPUTS = [
    {"action": "move_forward", "start": 1.0, "duration": 1.2},
    {"action": "move_right", "start": 2.4, "duration": 0.8},
    {"action": "jump", "start": 3.4, "duration": 0.2},
    {"action": "move_back", "start": 4.0, "duration": 0.8},
]


async def _playtest(ctx: RunContext, hub: MCPHub, label: str) -> dict:
    chk = await hub.call("godot_check", {})
    run = await hub.call("godot_run", {"seconds": float(config.get("godot.run_seconds", 6)),
                                       "inputs": PLAYTEST_INPUTS, "shot_times": [0.8, 3.0, 5.4], "label": label})
    track = run.get("player_track_sample", [])
    moved = 0.0
    if len(track) >= 2:
        p0, p1 = track[0]["pos"], max(track, key=lambda t: sum((a - b) ** 2 for a, b in zip(t["pos"], track[0]["pos"])))["pos"]
        moved = sum((a - b) ** 2 for a, b in zip(p0, p1)) ** 0.5
    errors = (sum(chk.get("script_errors", {}).values(), []) + chk.get("runtime_errors", []) + run.get("errors", []))
    probe = run.get("probe", {})
    hard_ok = not errors and bool(probe) and probe.get("players", 0) > 0 and moved > 0.5
    facts = (f"脚本/运行错误 {len(errors)} 个{('：' + '; '.join(errors[:4])) if errors else ''}；"
             f"player 组节点 {probe.get('players', 0)} 个；模拟输入下玩家最大位移 {moved:.2f} 米；"
             f"FPS {probe.get('fps', '?')}；节点数 {probe.get('node_count', '?')}")
    for p in run.get("images", []):
        ctx.artifact("screenshot", p, f"试玩截图 {label}", shot=label)
    return {"errors": errors, "moved": moved, "probe": probe, "images": run.get("images", []),
            "hard_ok": hard_ok, "facts": facts}


async def _review_play(ctx: RunContext, spec: dict, key_art: str | None, test: dict, pass_score: float) -> dict:
    if not test["images"]:
        return {"score": 0.0, "pass": False, "diffs": ["游戏没有成功运行出画面"] + test["errors"][:5],
                "next_actions": ["先修复所有脚本错误，确保 godot_run 能截图"]}
    return await vision.review_against_target(
        target=[key_art] if key_art else [], candidate=test["images"],
        subject=f"游戏《{spec.get('title', '')}》试玩截图（3 张，按时间顺序，期间模拟了前进/右移/跳跃/后退输入）",
        criteria=spec.get("acceptance", []), facts=test["facts"], pass_score=pass_score,
        style_context=style_agents.rules_text(ctx.options.get("style_profile"), "review") + consultation.revision_rules(ctx.options),
        model_key=route(ctx, "observe"), on_delta=ctx.delta_emitter())


async def stage_review(ctx: RunContext) -> None:
    spec = ctx.state["spec"]
    key_art = _target_image(ctx, "_key_art") if "_key_art" in ctx.state.get("concepts", {}) else None
    async with MCPHub(ctx.dir, godot_project=_godot_project(ctx), servers=["godot"]) as hub:
        test = await _playtest(ctx, hub, "final")
        if not test["probe"]:  # 偶发：窗口未完成探针（超时/被遮挡），重试一次
            ctx.emit("warn", "最终试玩没有拿到探针数据，重试一次")
            test = await _playtest(ctx, hub, "final_retry")
    rev = await _review_play(ctx, spec, key_art, test, float(ctx.opt("model_pass_score", 7.5)))
    ctx.state["final_review"] = {"review": rev, "test": {k: v for k, v in test.items() if k != "images"},
                                 "images": test["images"]}
    ctx.save()
    ctx.emit("review", f"最终验收：{rev['score']:.1f}/10；硬性检查 {'通过' if test['hard_ok'] else '未通过'}",
             {**rev, "facts": test["facts"]})


AMBIENCE_HINT = ('{"sounds": [{"name": "英文小写下划线文件名", "prompt": "英文声音描述（具体声源、距离、空间感，'
                 '不写音乐/人声）", "seconds": 4-20, "loop": true/false}]}')


async def _ambience(ctx: RunContext, spec: dict) -> list[dict]:
    """音效：LLM 按场景内容写环境音 / 音效描述 → 本机开源模型（MOSS-SoundEffect）生成 WAV。
    结果缓存在 state["audio"]（同一需求不重复生成）；音效服务未安装时记一条提醒并跳过，不影响交付。"""
    from .services import manager
    if not ctx.opt("audio", True):
        return []
    cached = ctx.state.get("audio") or {}
    if cached.get("sounds") and all(Path(s["path"]).exists() for s in cached["sounds"]):
        return cached["sounds"]
    svc = manager.services.get("audio")
    if svc is None or svc.missing():
        ctx.emit("warn", "音效未生成：本地音效模型未安装（双击 scripts/setup_audio.command 安装 MOSS-SoundEffect）")
        return []
    facts = {"title": spec.get("title"), "pitch": spec.get("pitch"), "summary": spec.get("summary") or spec.get("description"),
             "assets": [a.get("name") for a in spec.get("assets", [])],
             "lighting": "day" if not (ctx.state.get("compose") or {}).get("light_rig") else "measured daylight"}
    n = int(ctx.opt("audio_count", 3))
    def validate(o: Any) -> str | None:
        if not isinstance(o, dict) or not isinstance(o.get("sounds"), list) or not o["sounds"]:
            return "需要 {\"sounds\": [...]} 且非空"
        for s in o["sounds"]:
            if not isinstance(s, dict) or not s.get("name") or not s.get("prompt"):
                return "每条需要 name 和 prompt"
        return None
    ctx.emit("info", "LLM 正在为场景写环境音 / 音效描述…")
    plan = await LLM(route(ctx, "plan")).json(
        [{"role": "system", "content": f"你是游戏音效设计。按场景内容给出 {n} 条以内的声音：第一条是可循环的整体环境底噪"
                                       "（loop=true，12–20 秒），其余是场景里具体物体的点缀音效（loop=false，3–8 秒）。"
                                       "只描述画面里真实存在的声源；描述用英文，具体可听。"},
         {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
        validate=validate, schema_hint=AMBIENCE_HINT)
    adir = ctx.dir / "audio"
    adir.mkdir(parents=True, exist_ok=True)
    sounds = []
    for i, s in enumerate(plan["sounds"][:n]):
        name = "".join(c if c.isalnum() or c == "_" else "_" for c in str(s["name"]).lower())[:40] or f"sound_{i}"
        loop = bool(s.get("loop", i == 0))
        secs = max(3.0, min(20.0, float(s.get("seconds") or (16 if loop else 6))))
        path = adir / f"{i:02d}_{name}.wav"
        try:
            r = await vision.generate_audio(str(s["prompt"]), path, seconds=secs, seed=i, loop=loop)
        except Exception as e:  # noqa: BLE001 — 单条失败不影响其它
            ctx.emit("warn", f"音效 {name} 生成失败：{e}")
            continue
        sounds.append({"name": name, "prompt": s["prompt"], "loop": loop, "seconds": r.get("seconds", secs), "path": str(path)})
        ctx.emit("result", f"音效 {name}（{'循环' if loop else '单次'} {r.get('seconds', secs)} 秒，用时 {r.get('elapsed', '-')} 秒）")
    ctx.state["audio"] = {"sounds": sounds}
    ctx.save()
    return sounds


def _export_environment(ctx: RunContext, out: Path) -> list[str]:
    """把实测天空盒（PNG + HDR）与布光闭环得到的灯光组（rig）写进交付物，供 Godot / Blender 复用。"""
    comp = ctx.state.get("compose") or {}
    env = out / "environment"
    lines: list[str] = []
    files = [(comp.get("sky_dome"), "sky_dome.png"), (comp.get("sky_dome_hdr"), "sky_dome.hdr")]
    if not any(p and Path(p).exists() for p, _ in files) and not comp.get("light_rig"):
        return lines
    env.mkdir(parents=True, exist_ok=True)
    for src, name in files:
        if src and Path(src).exists():
            shutil.copy2(src, env / name)
    if comp.get("light_rig"):
        rig = {"rig": comp["light_rig"], "error": comp.get("light_err"), "exposure": comp.get("exposure"),
               "fit_target": (ctx.state.get("spec") or {}).get("light_fit"),
               "notes": "key/fill/rim: dir 为世界坐标下指向光源的单位向量（Blender 太阳光），color 为线性 RGB，energy 为 W/m²；"
                        "world 为环境光颜色与强度。"}
        (env / "light_rig.json").write_text(json.dumps(rig, ensure_ascii=False, indent=2))
    lines += ["", "## 环境与布光", "",
              f"- 天空盒：environment/sky_dome.png、sky_dome.hdr（由目标图天空 / 海面逐行实测，等距柱状投影）",
              f"- 灯光组：environment/light_rig.json（布光闭环，残差 {comp.get('light_err', '-')}）"]
    return lines


async def stage_deliver(ctx: RunContext) -> None:
    assembly.validate(ctx.options, ctx.dir)
    assembly.validate_delivery(ctx)
    quality = assess({**ctx.run, "options": ctx.options}, ctx.state)
    ctx.state["quality"] = quality
    ctx.save()
    if quality["status"] != "passed":
        reasons = "；".join(c["name"] for c in quality["issues"][:5])
        raise RuntimeError(f"交付门禁：{quality['label']}（{reasons}）。请在控制台质量检查页核对并修复；现有产物可用于预览。")
    spec = ctx.state["spec"]
    out = ctx.dir / "deliverables"
    comp = ctx.state.get("compose", {})
    if comp.get("rounds") and comp.get("champion") is not None:  # 交付门槛：组装质量不达标不交付
        min_score = float(ctx.opt("deliver_min_score", 6.0))
        ch = comp["rounds"][comp["champion"]]
        diffs = ((ch.get("observation") or {}).get("diffs")) or []
        ch_score = jev.ledger_score(diffs) if diffs else ch.get("score", 0)
        m = ch.get("metrics") or {}
        dino, siglip = m.get("dino"), m.get("siglip")
        # 只看评审分；客观相似度写进提示，不放行（2026-10-30 用户确认）
        if ch_score < min_score:
            # 旧交付物（如门槛上线前已生成的）移到 rejected，不再作为交付展示
            for old in (out, ctx.dir / f"{ctx.id}-deliverables.zip"):
                if old.exists():
                    old.rename(old.with_name(old.name.replace("deliverables", "deliverables.rejected")))
            raise RuntimeError(f"场景组装冠军第 {ch['round']} 轮评分 {ch_score}/10，低于交付门槛 {min_score}，"
                               f"（客观相似度 DINO {dino} / SigLIP {siglip}），不交付。"
                               "可增加 compose_max_rounds 或先改进子资产后从「场景组装」重试")
    osk = ctx.state.get("oskar") or {}
    bad = [c["name"] for c in osk.get("checks") or [] if c.get("required") and not c["passed"]]
    if bad:  # Oskar 模式交付门槛：最佳版本硬检查必须全部通过
        raise RuntimeError(f"Oskar 场景最佳版本硬检查未通过（{'、'.join(bad)}），不交付。可增加 oskar_rounds 后从「引擎构建与迭代」重试")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "models").mkdir()
    (out / "concepts").mkdir()
    lines = [f"# {spec.get('title', ctx.id)}", "", f"运行 ID：{ctx.id}", f"需求：{ctx.run['brief']}", ""]
    if spec.get("pitch"):
        lines += [f"**卖点**：{spec['pitch']}", ""]
    lines += ["## 资产", "", "| 资产 | 最终评分 | 轮数 | GLB |", "|---|---|---|---|"]
    for a in spec["assets"]:
        m = ctx.state.get("models", {}).get(a["id"], {})
        if m.get("glb"):
            shutil.copy2(m["glb"], out / "models" / Path(m["glb"]).name)
        try:
            shutil.copy2(_target_image(ctx, a["id"]), out / "concepts" / f"{a['id']}.png")
        except (KeyError, IndexError, OSError):
            pass
        lines.append(f"| {a['name']} | {m.get('final_score', '-')} | {len(m.get('rounds', []))} | "
                     f"{'models/' + Path(m['glb']).name if m.get('glb') else '未导出'} |")
    if ctx.run["mode"] == "oskar" and _godot_project(ctx).exists():
        shutil.copytree(_godot_project(ctx), out / "godot_project", ignore=shutil.ignore_patterns(".godot"))
        g = ctx.state.get("oskar", {}).get("godot", {})
        lines += ["", "## Godot 展示工程", "",
                  f"- 核对：{'；'.join(c['name'] + ('通过' if c['passed'] else '未通过') for c in g.get('checks', [])) or '-'}",
                  f"- 与 Blender 同机位相似度：{g.get('match') or '-'}；帧率 {g.get('fps', '-')}",
                  "- 操作：拖动 / A D 旋转，滚轮 / W S 缩放，E 切换日光 / 黄昏；地形与建筑已带碰撞体",
                  "- 玩法写在 godot_project/main.gd（extends addons/pipeline/oskar_diorama.gd，_ready 里先 super()）",
                  "", "```bash", f"{config.get('godot.binary')} --path godot_project", "```"]
    wd = ctx.state.get("world")
    if wd and wd.get("glb"):
        shutil.copy2(wd["glb"], out / "models" / Path(wd["glb"]).name)
        wm = wd.get("metrics") or {}
        lines += ["", "## Oskar 世界", "",
                  f"- 按关键画面生成（最佳第 {wd.get('best_round')} 轮），与关键画面 DINO {wm.get('dino', '-')} / SigLIP {wm.get('siglip', '-')}",
                  f"- 可站立点 {len(wd.get('walk') or [])} 个，通路 {len(wd.get('paths') or [])} 条；文件 models/{Path(wd['glb']).name}"]
    if ctx.run["mode"] in ("game", "assembly"):
        proj = _godot_project(ctx)
        if proj.exists():
            shutil.copytree(proj, out / "godot_project", ignore=shutil.ignore_patterns(".godot"))
        fr = ctx.state.get("final_review", {})
        rv = fr.get("review", {})
        lines += ["", "## 试玩验收", "", f"- 评分：{rv.get('score', '-')}/10",
                  f"- 客观数据：{fr.get('test', {}).get('facts', '-')}"]
        lines += [f"- 差异：{d}" for d in rv.get("diffs", [])[:6]]
        lines += ["", "## 运行", "", "```bash", f"{config.get('godot.binary')} --path godot_project", "```"]
    if comp.get("glb"):
        shutil.copy2(comp["glb"], out / "models" / "scene_composite.glb")
        if (ctx.dir / "layout.json").exists():
            shutil.copy2(ctx.dir / "layout.json", out / "layout.json")
        ch = comp["rounds"][comp["champion"]]
        lines += ["", "## 场景组装", "", f"- 冠军：第 {ch['round']} 轮，台账评分 {ch['score']}/10",
                  f"- 实例数：{len(ch['layout'])}；文件：models/scene_composite.glb、layout.json"]
    lines += _export_environment(ctx, out)
    if comp.get("glb") or ctx.options.get("audio"):
        sounds = await _ambience(ctx, spec)
        if sounds:
            lines += ["", "## 音效（MOSS-SoundEffect，开源，本机生成）", "", "| 文件 | 类型 | 时长 | 描述 |", "|---|---|---|---|"]
            for s in sounds:
                rel = f"audio/{Path(s['path']).name}"
                (out / "audio").mkdir(exist_ok=True)
                shutil.copy2(s["path"], out / rel)
                lines.append(f"| {rel} | {'循环环境音' if s['loop'] else '单次音效'} | {s['seconds']} 秒 | {s['prompt']} |")
    if ctx.run["mode"] == "atlas":
        atlas_state = ctx.state.get("atlas") or {}
        w = atlas_state.get("world") or {}
        if w.get("glb"):
            shutil.copy2(w["glb"], out / "models" / Path(w["glb"]).name)
        for name in ("bible.json", "world_layout.json"):
            if (ctx.dir / "atlas" / name).exists():
                shutil.copy2(ctx.dir / "atlas" / name, out / name)
        if _godot_project(ctx).exists():
            shutil.copytree(_godot_project(ctx), out / "godot_project", ignore=shutil.ignore_patterns(".godot"))
        lines += world_atlas.report_lines(ctx.state)
        lines += ["", "## 运行", "", "```bash", f"{config.get('godot.binary')} --path godot_project", "```"]
    if ctx.run["mode"] == "assembly":
        shutil.copy2(ctx.dir / "assembly-manifest.json", out / "assembly-manifest.json")
        lines += ["", "## 来源与串联", ""]
        lines += [f"- {i}. {p['title']}（{assembly.ROLES[p['role']]}）· {p['source_id']}"
                  for i, p in enumerate(ctx.options["assembly"]["parts"], 1)]
    target_proj = ctx.options.get("integrate_to")
    if target_proj:
        tp = Path(target_proj).expanduser()
        if not (tp / "project.godot").exists():
            ctx.emit("warn", f"integrate_to 不是 Godot 工程目录: {tp}")
        else:
            dest = tp / "assets" / "pipeline" / ctx.id
            ctx.emit("approval", f"即将把 {len(list((out / 'models').glob('*.glb')))} 个 GLB 复制到 {dest}，"
                                 "需人工确认（不会覆盖原有文件）", {"gate": "integrate"})
            ctx.options.setdefault("approval_gates", [])
            gates = ctx.opt("approval_gates", []) or []
            if "integrate" not in gates:
                ctx.options["approval_gates"] = list(gates) + ["integrate"]
            await ctx.wait_approval("integrate")
            dest.mkdir(parents=True, exist_ok=True)
            for glb in (out / "models").glob("*.glb"):
                shutil.copy2(glb, dest / glb.name)
            lines += ["", "## 接入", "", f"GLB 已复制到 `{dest}`（新目录，未覆盖原文件；场景引用需另行接入并验收）。"]
            ctx.emit("result", f"已接入目标工程：{dest}")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n")
    archive = shutil.make_archive(str(ctx.dir / f"{ctx.id}-deliverables"), "zip", out)
    ctx.artifact("report", out / "REPORT.md", "交付报告")
    ctx.artifact("package", archive, "交付包 (zip)")
    ctx.emit("result", f"交付完成：{archive}")


# ───────────────────────────── 场景拆分模式 ─────────────────────────────

def _normalize_camera(cam: Any, aspect: float) -> dict | None:
    """拆分给出的参考机位：三个数的 eye/target、合理的视场角；不合格就不用（组装只用标准机位）。"""
    try:
        eye = [float(x) for x in cam["eye"]][:3]
        tgt = [float(x) for x in cam["target"]][:3]
        if len(eye) != 3 or len(tgt) != 3 or sum((a - b) ** 2 for a, b in zip(eye, tgt)) < 0.01:
            return None
        fov = float(cam.get("fov_deg", 40))
        return {"eye": eye, "target": tgt, "fov_deg": max(15.0, min(90.0, fov)), "aspect": round(float(aspect), 4)}
    except (TypeError, KeyError, ValueError):
        return None


def _backdrop_color(img) -> list[float]:
    """参考图上部 35% 的平均色（天空/远景），作为组装渲染的背景色，让同机位对比不被灰色摄影棚干扰。"""
    top = img.crop((0, 0, img.width, max(1, int(img.height * 0.35)))).resize((32, 8))
    px = list(top.getdata())
    return [round(sum(c[i] for c in px) / len(px) / 255, 4) for i in range(3)]



def _default_placement(_a: dict | None = None) -> dict:
    """漏项 / 拆分缺 placements 时的占位；深度定位会按 SAM 实例改写坐标。"""
    return {"at": [0, 0, 0], "rot_z": 0, "scale": 1}


def _ensure_placements(a: dict) -> None:
    """缺 placements 时自动补默认，避免整段漏项补全因校验失败被跳过。"""
    pls = a.get("placements")
    if not isinstance(pls, list) or not pls:
        a["placements"] = [_default_placement(a)]
        return
    fixed = []
    for p in pls:
        if not isinstance(p, dict):
            fixed.append(_default_placement(a))
            continue
        p.setdefault("at", [0, 0, 0])
        p.setdefault("rot_z", 0)
        p.setdefault("scale", 1)
        fixed.append(p)
    a["placements"] = fixed


# 过窄的 SAM 提示（实测 red spike / white daisy / green leafy 常 found=0）→ 更可分割的说法 + 口语 alts
_SAM_BROADEN = {
    "red spike flower": ("red flower cluster", ["red flower", "flower bush", "flower"]),
    "white daisy flower": ("white flower", ["daisy", "white blossom", "flower"]),
    "green leafy shrub": ("green bush", ["shrub", "foliage", "bush"]),
    "stream waterfall water": ("waterfall", ["stream", "waterfall cascade", "creek"]),
    "stream waterfall": ("waterfall", ["stream", "waterfall cascade", "creek"]),
}


def _broaden_sam_prompt(a: dict) -> None:
    """把过窄的 sam_prompt 换成更可分割的英文，并确保 alts 含常见名词。"""
    pr = str(a.get("sam_prompt") or a.get("name") or a.get("id") or "").strip()
    key = pr.lower()
    alts = [str(x).strip() for x in (a.get("sam_alts") or []) if str(x).strip()]
    if key in _SAM_BROADEN:
        new_pr, extra = _SAM_BROADEN[key]
        a["sam_prompt"] = new_pr
        for e in extra:
            if e.lower() not in {x.lower() for x in alts} and e.lower() != new_pr.lower():
                alts.append(e)
    words = [w for w in re.findall(r"[A-Za-z]+", a.get("sam_prompt") or pr) if len(w) > 2]
    for w in reversed(words):
        if w.lower() not in {x.lower() for x in alts}:
            alts.insert(0, w.lower())
            break
    a["sam_alts"] = alts[:4]


def _validate_decompose(o: Any) -> str | None:
    if not isinstance(o, dict) or not isinstance(o.get("assets"), list):
        return "需要 assets 数组"
    if not 2 <= len(o["assets"]) <= 14:
        return "子资产需要 2–14 种"
    ids = set()
    for a in o["assets"]:
        if not a.get("id") or a["id"] in ids:
            return f"子资产 id 缺失或重复: {a.get('id')}"
        ids.add(a["id"])
        b = a.get("bbox")
        if isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b) and max(b) > 1:
            b = a["bbox"] = [v / 1000 for v in b]  # Qwen 系视觉模型原生 0–1000 相对坐标 → 归一化
        if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)
                and 0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1):
            return f"{a['id']}.bbox 必须是 0–1000 的 [x0,y0,x1,y1]，且 x0<x1、y0<y1"
        size = a.get("size_m")
        if not (isinstance(size, list) and len(size) == 3):
            return f"{a['id']}.size_m 必须是 3 个数字"
        _ensure_placements(a)
        _broaden_sam_prompt(a)
        a.setdefault("sam_prompt", a.get("name") or a["id"])
    return None


async def stage_decompose(ctx: RunContext) -> None:
    from PIL import Image, ImageDraw
    refs = [a for a in ctx.store.artifacts(ctx.id) if a["kind"] == "reference"]
    if refs:
        target = refs[0]["path"]
    else:  # 没有参考图时先用 FLUX 生成一张场景目标图
        ctx.emit("warn", "没有找到参考图/项目目标图，改用 FLUX 按需求生成场景目标图")
        target = str(ctx.dir / "concept" / "_scene" / "target.png")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        # FLUX 只懂英文：中文需求直接喂进去会画出无关的海报（实测出现过带水印的寺庙图），先写成英文画面描述
        prompt = await _english_prompt(ctx, ctx.run["brief"], "整个游戏场景的构图、主体、配色")
        await vision.generate_image(prompt + ", game environment concept art, wide shot, no text, no watermark", Path(target))
        ctx.artifact("concept", target, "场景目标图（FLUX 按需求生成）", asset="_scene", prompt=prompt)
    size = ctx.opt("scene_size", "") or "按需求描述"
    image3d = ctx.run.get("mode") == "scene3d"
    msgs = [{"role": "system", "content": prompts.DECOMPOSE_SYSTEM + (prompts.DECOMPOSE_IMAGE3D if image3d else "")},
            user_message(prompts.DECOMPOSE_TASK.format(brief=ctx.run["brief"], size=size), [target], max_side=1536)]
    ctx.emit("info", "LLM 正在把目标图拆分为子资产…")
    spec = await LLM(route(ctx, "decompose")).json(msgs, on_delta=ctx.delta_emitter(), thinking_budget=6144,
                                                   validate=_validate_decompose, retries=2)
    floors = {"character": 8000, "vehicle": 3000, "building": 3000, "environment": 3000, "prop": 1500}
    img = Image.open(target).convert("RGB")
    W, H = img.size
    spec["camera"] = _normalize_camera(spec.get("camera"), W / H)
    spec["backdrop"] = _backdrop_color(img)
    # SAM 3 实例分割：每类子资产找出全部实例（掩码/框/数量），取代 VLM 单次框选；不设人工确认
    prompts_by_asset = {a["id"]: str(a.get("sam_prompt") or a["name"]).strip() for a in spec["assets"]}
    ctx.emit("info", "SAM 3 正在按文字提示分割各类子资产的实例…")
    seg = await vision.segment_instances(target, sorted(set(prompts_by_asset.values())),
                                         ctx.dir / "concept" / "_scene" / "sam")
    if seg is None:
        ctx.emit("warn", "SAM 3 实例分割不可用，退回 LLM 框选")
    else:
        seg = await _verify_sam(ctx, img, target, spec["assets"], prompts_by_asset, seg)
    ann = img.copy()
    draw = ImageDraw.Draw(ann)
    palette = [(230, 60, 60), (40, 160, 70), (40, 110, 230), (230, 160, 30), (160, 60, 200), (30, 180, 190),
               (200, 90, 40), (90, 90, 90)]
    concepts = ctx.state.setdefault("concepts", {})
    masks_by: dict[str, list[dict]] = {}   # 资产 → SAM 实例掩码（给深度定位用）
    rep_masks: dict[str, str] = {}         # 资产 → 代表实例掩码（给框选裁剪抹掉别的物件用）
    box_crops: list[tuple[dict, Path]] = []
    for i, a in enumerate(spec["assets"]):
        a.setdefault("kind", "prop")
        a.setdefault("role", "decor")
        if image3d:
            a["method"] = a.get("method") if a.get("method") in ("image3d", "blueprint", "terrain", "backdrop") else "image3d"
        else:
            a.pop("method", None)
        a["image_prompt"] = a.get("image_prompt") or "(目标图裁剪)"
        a["poly_budget"] = max(int(a.get("poly_budget") or 8000), floors.get(a["kind"], 1500))
        insts = (seg or {}).get(prompts_by_asset[a["id"]]) or []
        a["sam"] = {"prompt": prompts_by_asset[a["id"]].replace("#verified", ""), "found": len(insts),
                    **({"verify": a.pop("sam_verify")} if a.get("sam_verify") else {})}
        a["instances"] = [{k: x[k] for k in ("box", "score", "area")} for x in insts]
        is_terrain = a.get("method") == "terrain" or a.get("kind") == "environment"
        # 重复物件：LLM 常只给 1 个 placement，把占位补到与 SAM 实例数一致，深度定位再写成实测坐标
        if insts and not is_terrain and a.get("method") != "backdrop" and a.get("kind") != "backdrop":
            seed = dict(a["placements"][0]) if a.get("placements") else {"at": [0, 0, 0], "rot_z": 0, "scale": 1}
            while len(a["placements"]) < len(insts):
                a["placements"].append({**seed})
        if insts:   # 深度定位按这个顺序取实例：核验过的代表实例在前，其余按与拆分框的吻合度
            first = _terrain_rep(insts) if is_terrain else insts[0] if prompts_by_asset[a["id"]].endswith("#verified") else \
                max(insts, key=lambda x: _rep_score(x, a["bbox"], W, H))
            rest = sorted((x for x in insts if x is not first), key=lambda x: -_rep_score(x, a["bbox"], W, H))
            masks_by[a["id"]] = [{"mask": x["mask"], "score": x.get("score", 0)} for x in [first, *rest] if x.get("mask")]
        c = palette[i % len(palette)]
        out = ctx.dir / "concept" / a["id"] / "crop.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        if insts:  # 代表实例：置信度高、面积不过小、与 LLM 框选提示重叠者优先
            rep = first
            a.setdefault("bbox_llm", a["bbox"])   # 拆分模型给的原框：深度定位在 SAM 只认出一小块时用它
            rep_masks[a["id"]] = rep["mask"]
            a["bbox"] = rep["box"]
            a["sam"]["score"] = rep["score"]
            _masked_crop(img, Image.open(rep["mask"]), out)
            label = f"{a['name']}（SAM 实例抠图，共 {len(insts)} 个实例）"
            for x in insts:
                bx = x["box"]
                draw.rectangle([int(bx[0] * W), int(bx[1] * H), int(bx[2] * W), int(bx[3] * H)], outline=c,
                               width=max(1, W // 600))
        else:
            if seg is not None:
                ctx.emit("warn", f"{a['name']}：SAM 没有找到 “{prompts_by_asset[a['id']]}”，退回 LLM 框选裁剪")
            x0, y0, x1, y1 = a["bbox"]
            m = 0.03
            img.crop((int(max(0, x0 - m) * W), int(max(0, y0 - m) * H), int(min(1, x1 + m) * W),
                      int(min(1, y1 + m) * H))).save(out)
            box_crops.append((a, out))
            label = f"{a['name']}（目标图裁剪）"
        x0, y0, x1, y1 = a["bbox"]
        aid = ctx.artifact("concept", out, label, asset=a["id"], index=0, bbox=a["bbox"],
                           selected=True, placements=len(a["placements"]), instances=len(insts))
        concepts[a["id"]] = {"prompt": "(目标图裁剪)", "selected": 0, "candidates": [{"path": str(out), "artifact": aid}]}
        draw.rectangle([int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)], outline=c, width=max(3, W // 250))
        draw.text((int(x0 * W) + 6, int(y0 * H) + 4),
                  f"{i + 1}. {a['id']} ×{len(a['placements'])}" + (f" (SAM {len(insts)})" if seg is not None else ""), fill=c)
    # 两个资产认领了同一个 SAM 实例（实测：「windmill」的提示词认出的是灯塔上的小风车叶，代表实例就成了整座灯塔）：
    # 像素重叠超过较小者一半时，代表实例与拆分框吻合差的那个退回拆分框裁剪
    for oa, ob_ in _rep_conflicts(rep_masks, spec["assets"]):
        loser = min((oa, ob_), key=lambda a: _box_iou(a["bbox"], a.get("bbox_llm") or a["bbox"]))
        other = ob_ if loser is oa else oa
        ctx.emit("warn", f"{loser['name']}：SAM 代表实例与「{other['name']}」是同一块（像素重叠过半），"
                         f"改用拆分框裁剪")
        rep_masks.pop(loser["id"], None)
        masks_by.pop(loser["id"], None)
        loser["bbox"] = loser.get("bbox_llm") or loser["bbox"]
        out = ctx.dir / "concept" / loser["id"] / "crop.png"
        box_crops.append((loser, out))
    # 框选裁剪里常常混着别的物件（例如风车屋的框里有半座灯塔，图生 3D 就会再做出一座灯塔）：
    # 用其他资产的 SAM 掩码把它们抹成灰底，只留这个物件。
    for a, out in box_crops:
        if a.get("method") in ("terrain", "backdrop") or not rep_masks:
            continue
        clean = img.copy()
        grey = Image.new("RGB", img.size, (116, 120, 126))
        for oid, mpath in rep_masks.items():
            if oid != a["id"]:
                clean.paste(grey, (0, 0), Image.open(mpath).convert("L").resize(img.size).point(lambda v: 255 if v > 127 else 0))
        x0, y0, x1, y1 = a["bbox"]
        m = 0.03
        clean.crop((int(max(0, x0 - m) * W), int(max(0, y0 - m) * H), int(min(1, x1 + m) * W),
                    int(min(1, y1 + m) * H))).save(out)
    ann_path = ctx.dir / "concept" / "_scene" / "decompose.jpg"
    ann_path.parent.mkdir(parents=True, exist_ok=True)
    ann.save(ann_path, quality=90)
    concepts["_scene"] = {"prompt": "(场景目标图)", "selected": 0, "candidates": [
        {"path": target, "artifact": refs[0]["id"] if refs else ctx.artifact("concept", target, "场景目标图")}]}
    ctx.artifact("decompose", ann_path, "拆分示意（粗框 = 子资产目标实例，细框 = SAM 找到的全部实例）",
                 assets=[a["id"] for a in spec["assets"]])
    if image3d and ctx.opt("depth_layout", True) and masks_by:
        await _depth_layout(ctx, spec, target, masks_by, W / H)
        # 自检未过（未归属 >30% 或区域颜色不合常理）→ 漏项补全 + 重新圈一次
        masks_by, prompts_by_asset, seg = await _recircle_after_audit(
            ctx, img, target, spec, masks_by, prompts_by_asset, seg, W / H)
    ctx.state["spec"] = spec
    # 重新拆分 = 新的子资产清单：旧的子资产模型和组装记录都对不上了，清掉，否则组装会拿旧布局里的资产名去找 GLB
    ctx.state.pop("models", None)
    ctx.state.pop("compose", None)
    ctx.state["scene_target"] = target
    ctx.save()
    (ctx.dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2))
    ctx.artifact("spec", ctx.dir / "spec.json", spec.get("title", "场景拆分方案"))
    if spec.get("title"):
        ctx.store.update_run(ctx.id, title=spec["title"])
    ctx.emit("result", f"拆分为 {len(spec['assets'])} 种子资产、"
             f"{sum(len(a['placements']) for a in spec['assets'])} 个实例：" +
             "、".join(f"{a['name']}×{len(a['placements'])}" + (f"（SAM 实例 {a['sam']['found']}）" if seg is not None else "")
                      for a in spec["assets"]), {"spec": spec})
    await ctx.wait_approval("decompose")


def _palette_sheet(pm: dict, out: Path) -> Path | None:
    """调色板小样：每行一个区域 / 物件：主色、受光、背光、色团，细高物件再画色带。"""
    try:
        from PIL import Image, ImageDraw
        rows = [(_region_cn(k), v) for k, v in pm.items() if isinstance(v, dict) and v.get("base")] + [
               (k, v) for k, v in (pm.get("assets") or {}).items()]
        im = Image.new("RGB", (820, 34 * len(rows) + 10), "white")
        d = ImageDraw.Draw(im)
        for i, (name, v) in enumerate(rows):
            y = 6 + 34 * i
            d.text((8, y + 10), name[:18], fill="black")
            for j, key in enumerate(("base", "lit", "shade")):
                d.rectangle([170 + 70 * j, y, 230 + 70 * j, y + 28], fill=v.get(key, "#FFFFFF"))
            for j, c in enumerate((v.get("clusters") or v.get("swatches") or [])[:3]):
                d.rectangle([400 + 36 * j, y + 4, 430 + 36 * j, y + 28], fill=c.get("hex", "#FFFFFF"))
            for b in v.get("bands") or []:
                x0, x1 = 520 + int(280 * b["from"]), 520 + int(280 * b["to"])
                d.rectangle([x0, y, max(x0 + 1, x1), y + 28], fill=b["hex"])
        im.save(out)
        return out
    except Exception:  # noqa: BLE001
        return None


async def _depth_layout(ctx: RunContext, spec: dict, target: str, masks_by: dict, aspect: float) -> None:
    """本地度量深度（默认 Depth Anything 3 度量版，焦距取 MoGe-2 估的内参）+ SAM 3 掩码 → 尺寸、摆放、相机、支撑平面。
    LLM 只决定「有哪些物件」和朝向；量不到的物件按锚点比例换算。失败时保留 LLM 的估计并告警。
    计算在 envs/depth 的子进程里做（控制台环境没有 numpy），这里只交换 JSON。"""
    import subprocess
    model = ctx.opt("depth_model", "da3")
    out = ctx.dir / "concept" / "_scene" / "depth"
    out.mkdir(parents=True, exist_ok=True)
    req, res = out / "request.json", out / "result.json"
    req.write_text(json.dumps({"image": target, "spec": spec, "masks": masks_by, "aspect": aspect,
                               "model": model, "work_dir": str(out)}, ensure_ascii=False))
    res.unlink(missing_ok=True)
    py = config.ROOT / "envs" / "depth" / "bin" / "python"
    ctx.emit("info", f"深度定位：{'Depth Anything 3（MoGe-2 内参）' if model == 'da3' else 'MoGe-2'} 正在测量目标图…")
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        r = await asyncio.to_thread(subprocess.run, [str(py), str(Path(__file__).with_name("depth_layout.py")), str(req), str(res)],
                                    cwd=config.ROOT, env=env, capture_output=True, text=True, timeout=1800)
        (out / "run.log").write_text(f"exit={r.returncode}\n--- stdout\n{r.stdout}\n--- stderr\n{r.stderr}")
        if r.returncode != 0 or not res.exists():
            tail = [l for l in (r.stderr or r.stdout or "").splitlines() if l.strip() and "Warning" not in l][-4:]
            raise RuntimeError(f"退出码 {r.returncode}：" + " | ".join(tail)[-500:] + f"（详见 {out / 'run.log'}）")
        data = json.loads(res.read_text())
    except Exception as e:  # noqa: BLE001 — 深度失败不阻塞拆分，退回 LLM 估计
        ctx.emit("warn", f"深度定位失败，沿用 LLM 估计的尺寸和摆放：{type(e).__name__}: {str(e)[:400]}")
        return
    spec.clear()
    spec.update(data["spec"])
    summary, meas = data["summary"], data["measure"]
    cam = spec["camera"]
    ctx.emit("result", f"深度定位完成（{data['how']['depth']}）：锚点 {summary['anchor']}，LLM 尺寸 × {summary['scale_vs_llm']}；"
             f"实测 {len(summary['measured'])} 类、按比例换算 {len(summary['scaled_only'])} 类；"
             f"相机高 {cam['eye'][2]:.1f} m、俯角 {meas['camera']['pitch_down_deg']:.1f}°、视角 {cam['fov_deg']:.1f}°",
             {"depth_layout": spec["depth_layout"]})
    if (out / "layout_preview.png").exists():
        ctx.artifact("decompose", out / "layout_preview.png", "深度定位：俯视 / 侧视（实线 = 深度实测，虚线 = 按比例换算）")
    pm = spec.get("palette_measured") or {}
    if pm.get("error"):
        ctx.emit("warn", f"配色环节失败，沿用引擎默认配色：{pm['error'][:200]}")
    elif pm:
        sw = _palette_sheet(pm, out / "palette.png")
        rock = (pm.get("terrain_rock") or pm.get("terrain") or {}).get("base")
        ctx.emit("result", f"配色环节：崖壁 {rock}、岛顶 {(pm.get('terrain_top') or {}).get('base')}、"
                           f"水面 {(pm.get('water') or {}).get('base')}、天空 {(pm.get('sky') or {}).get('base')}；"
                           f"崖壁{'单色（只做明暗）' if pm.get('terrain_mode') == 'mono' else '多色'}；"
                           f"{len(pm.get('assets') or {})} 个物件各自取色"
                           + (f"（{'、'.join(k for k, v in pm['assets'].items() if v.get('bands'))} 按色带）"
                              if any(v.get('bands') for v in (pm.get('assets') or {}).values()) else ""),
                 {"palette_measured": {k: v for k, v in pm.items() if k != "regions"}})
        if sw:
            ctx.artifact("decompose", sw, "配色环节：从目标图实测的调色板（主色 / 受光 / 背光，色带）")
        cov = pm.get("coverage") or {}
        thr = float(cov.get("threshold") or 0.3)
        if cov.get("warn"):
            ctx.emit("warn", f"拆分自检：画面 {cov['unassigned'] * 100:.0f}% 的像素没有归到任何物件 / 地形 / 水 / 天"
                             f"（阈值 {thr * 100:.0f}%），取色看不到这些地方；将触发重新圈", cov)
        for name, why in (pm.get("suspect") or {}).items():
            ctx.emit("warn", f"拆分自检：{_REGION_CN.get(name, name)}区域不可信，不参与调色与验收：{why}；将触发重新圈")
        if pm.get("needs_retry"):
            pm["retry_reasons"] = [
                *( [f"未归属像素 {cov['unassigned'] * 100:.0f}%"] if cov.get("warn") else [] ),
                *[f"{_REGION_CN.get(n, n)}：{w}" for n, w in (pm.get("suspect") or {}).items()],
            ]



SPLIT_GAP_FILL_SYSTEM = """你是技术美术总监。场景拆分的自检没通过：有大片像素没被任何物件圈到，或某些区域颜色量得不合理。
请看目标图和当前子资产清单，只输出需要【新增】的子资产（不要重复已有 id / 不要改已有项）。
原则：
- 补上画面上能单独辨认、但清单漏掉的类别（路径、门、栏杆、小动物、独特花种、石板路、成片但主色不同的花草、溪流瀑布等）；
- 颜色明显不同的植物 / 花要分开成不同子资产；
- 【必填】每个新增资产必须有 placements 数组，至少 1 项：[{"at":[x,y,z],"rot_z":0,"scale":1}]；重复物件多给几个，数量尽量贴近画面里看得见的实例数；
- sam_prompt 用简短英文名词短语（2–4 词，优先常见可分割词，如 "waterfall"、"red flower"、"white flower"、"green bush"，不要用过细的植物学名）；
- sam_alts 给 2–3 个更口语的说法（优先单词，如 "flower"、"bush"、"stream"）；
- bbox 给该新资产一个代表性实例（0–1000 整数 [x0,y0,x1,y1]）；
- size_m 必须是 3 个数；
- 不要输出文字水印、标题、装饰边框。
只输出 JSON：{"assets":[{"id":"...","name":"...","kind":"prop","role":"decor","description":"...","sam_prompt":"...","sam_alts":["..."],"bbox":[x0,y0,x1,y1],"size_m":[x,y,z],"placements":[{"at":[0,0,0],"rot_z":0,"scale":1}],"method":"image3d"}]}。"""


def _split_retry_reasons(pm: dict) -> list[str]:
    """从 palette_measured 取出触发重新圈的原因（未归属 >≈30% 或区域色异常）。"""
    if not isinstance(pm, dict) or pm.get("error"):
        return []
    if pm.get("retry_reasons"):
        return list(pm["retry_reasons"])
    reasons = []
    cov = pm.get("coverage") or {}
    if cov.get("warn"):
        thr = float(cov.get("threshold") or 0.3)
        reasons.append(f"未归属像素 {float(cov.get('unassigned') or 0) * 100:.0f}%（阈值 {thr * 100:.0f}%）")
    for name, why in (pm.get("suspect") or {}).items():
        reasons.append(f"{_REGION_CN.get(name, name)}：{why}")
    if pm.get("needs_retry") and not reasons:
        reasons.append("拆分自检未通过")
    return reasons


def _validate_gap_assets(o: Any, existing_ids: set[str]) -> str | None:
    if not isinstance(o, dict) or not isinstance(o.get("assets"), list):
        return "需要 {\"assets\": [...]}"
    ids = set(existing_ids)
    for a in o["assets"]:
        if not isinstance(a, dict) or not a.get("id"):
            return "每个新增资产需要 id"
        if a["id"] in ids:
            return f"id 与已有或本组重复: {a['id']}"
        ids.add(a["id"])
        b = a.get("bbox")
        if isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b) and max(b) > 1:
            b = a["bbox"] = [v / 1000 for v in b]
        if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)
                and 0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1):
            return f"{a['id']}.bbox 非法"
        if not (isinstance(a.get("size_m"), list) and len(a["size_m"]) == 3):
            return f"{a['id']}.size_m 必须是 3 个数"
        _ensure_placements(a)  # 缺 placements 时自动补默认，不再整段失败
        a.setdefault("name", a["id"])
        a.setdefault("kind", "prop")
        a.setdefault("role", "decor")
        a.setdefault("sam_prompt", a["name"])
        _broaden_sam_prompt(a)
    if len(existing_ids) + len(o["assets"]) > 14:
        return "补全后子资产超过 14 种，请只保留最关键的漏项"
    return None


async def _recircle_after_audit(ctx: RunContext, img, target: str, spec: dict, masks_by: dict,
                                prompts_by_asset: dict[str, str], seg: dict | None, aspect: float):
    """自检失败时：漏项补全 → 对新资产重新 SAM 圈像素 → 再跑一次深度 / 取色。最多重圈 1 次。"""
    from PIL import Image, ImageDraw
    pm = spec.get("palette_measured") or {}
    reasons = _split_retry_reasons(pm)
    if not reasons:
        return masks_by, prompts_by_asset, seg
    ctx.emit("warn", "拆分自检未过，触发重新圈：" + "；".join(reasons), {"retry_reasons": reasons})
    existing = {a["id"] for a in spec["assets"]}
    inventory = [{"id": a["id"], "name": a.get("name"), "sam_prompt": a.get("sam_prompt") or a.get("name"),
                  "kind": a.get("kind"), "placements": len(a.get("placements") or [])} for a in spec["assets"]]
    filled = {"assets": []}
    try:
        def _gap_ok(o):
            if not isinstance(o, dict) or not isinstance(o.get("assets"), list):
                return "需要 assets 数组（每个资产必须含 id/bbox/size_m/placements）"
            if len(o["assets"]) == 0:
                return None  # 允许「确实没有漏项」
            return _validate_gap_assets(o, existing)

        filled = await LLM(route(ctx, "decompose")).json(
            [{"role": "system", "content": SPLIT_GAP_FILL_SYSTEM},
             user_message(
                 f"自检原因：{'；'.join(reasons)}\n当前清单：{json.dumps(inventory, ensure_ascii=False)}\n"
                 f"请补上漏掉的子资产。每个新增资产必须带 placements（至少一项 {{at,rot_z,scale}}）。",
                 [target], max_side=1536)],
            thinking_budget=2048, retries=2, validate=_gap_ok)
    except Exception as e:  # noqa: BLE001 — 漏项失败仍继续对 found=0 的旧资产重新圈
        ctx.emit("warn", f"漏项补全失败，仍继续对实例偏少的资产重新圈：{type(e).__name__}: {str(e)[:200]}")
        filled = {"assets": []}

    new_assets = [a for a in (filled.get("assets") or []) if a.get("id") not in existing]
    for a in new_assets:
        _ensure_placements(a)
        _broaden_sam_prompt(a)
    if not new_assets and not (pm.get("coverage") or {}).get("warn") and not pm.get("suspect"):
        return masks_by, prompts_by_asset, seg

    # 即便 LLM 没补出新类，也对「实例明显偏少」的重复物件再 SAM 一次（换口语提示）
    image3d = ctx.run.get("mode") == "scene3d"
    W, H = img.size
    floors = {"character": 8000, "vehicle": 3000, "building": 3000, "environment": 3000, "prop": 1500}
    concepts = ctx.state.setdefault("concepts", {})
    added = []
    for a in new_assets:
        if image3d:
            a["method"] = a.get("method") if a.get("method") in ("image3d", "blueprint", "terrain", "backdrop") else "image3d"
        a["image_prompt"] = a.get("image_prompt") or "(目标图裁剪)"
        a["poly_budget"] = max(int(a.get("poly_budget") or 8000), floors.get(a.get("kind"), 1500))
        prompts_by_asset[a["id"]] = str(a.get("sam_prompt") or a["name"]).strip()
        spec["assets"].append(a)
        added.append(a["id"])

    # 收集需要（重新）分割的提示：新资产 + 实例数明显少于 placements 预期、或 found<=1 但描述像成片植物的
    redo_prompts = []
    for a in spec["assets"]:
        pr = prompts_by_asset.get(a["id"]) or str(a.get("sam_prompt") or a["name"]).strip()
        prompts_by_asset[a["id"]] = pr
        found = int((a.get("sam") or {}).get("found") or len(a.get("instances") or []))
        alts = [str(x).strip() for x in (a.get("sam_alts") or []) if str(x).strip()][:3]
        if a["id"] in added or found <= 1:
            redo_prompts.extend([pr] + alts)

    redo_prompts = list(dict.fromkeys(p for p in redo_prompts if p))
    if redo_prompts:
        ctx.emit("info", f"重新圈：SAM 再分割 {len(redo_prompts)} 个提示（新增 {len(added)} 类）…")
        more = await vision.segment_instances(target, redo_prompts, ctx.dir / "concept" / "_scene" / "sam_retry")
        if more:
            seg = dict(seg or {})
            seg.update(more)
            if added:
                # 只对新资产跑核验，避免打乱已有代表实例
                new_only = [a for a in spec["assets"] if a["id"] in added]
                seg = await _verify_sam(ctx, img, target, new_only, prompts_by_asset, seg)

    # 刷新 masks / 裁剪 / placements
    for a in spec["assets"]:
        pr = prompts_by_asset[a["id"]]
        insts = (seg or {}).get(pr) or []
        a["sam"] = {**(a.get("sam") or {}), "prompt": pr.replace("#verified", ""), "found": len(insts)}
        a["instances"] = [{k: x[k] for k in ("box", "score", "area")} for x in insts]
        is_terrain = a.get("method") == "terrain" or a.get("kind") == "environment"
        if insts and not is_terrain and a.get("method") != "backdrop" and a.get("kind") != "backdrop":
            seed = dict(a["placements"][0]) if a.get("placements") else {"at": [0, 0, 0], "rot_z": 0, "scale": 1}
            while len(a["placements"]) < len(insts):
                a["placements"].append({**seed})
        if not insts:
            continue
        first = _terrain_rep(insts) if is_terrain else insts[0] if pr.endswith("#verified") else \
            max(insts, key=lambda x: _rep_score(x, a.get("bbox") or [0, 0, 1, 1], W, H))
        rest = sorted((x for x in insts if x is not first), key=lambda x: -_rep_score(x, a.get("bbox") or [0, 0, 1, 1], W, H))
        masks_by[a["id"]] = [{"mask": x["mask"], "score": x.get("score", 0)} for x in [first, *rest] if x.get("mask")]
        if a["id"] in added or a["id"] not in concepts:
            out = ctx.dir / "concept" / a["id"] / "crop.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            a.setdefault("bbox_llm", a.get("bbox"))
            a["bbox"] = first["box"]
            _masked_crop(img, Image.open(first["mask"]), out)
            aid = ctx.artifact("concept", out, f"{a['name']}（重新圈 SAM 抠图，共 {len(insts)} 个实例）",
                               asset=a["id"], index=0, bbox=a["bbox"], selected=True,
                               placements=len(a["placements"]), instances=len(insts))
            concepts[a["id"]] = {"prompt": "(目标图裁剪)", "selected": 0,
                                 "candidates": [{"path": str(out), "artifact": aid}]}

    if masks_by:
        await _depth_layout(ctx, spec, target, masks_by, aspect)
    pm2 = spec.get("palette_measured") or {}
    reasons2 = _split_retry_reasons(pm2)
    if reasons2:
        ctx.emit("warn", "重新圈后自检仍有问题（不再自动重试）：" + "；".join(reasons2),
                 {"retry_reasons": reasons2})
    else:
        ctx.emit("result", "重新圈完成，拆分自检已通过" + (f"；新增 {', '.join(added)}" if added else ""))
    return masks_by, prompts_by_asset, seg


STOP_WORDS = {"a", "an", "the", "of", "with", "and", "on", "in", "small", "large", "big", "tall"}


async def _verify_sam(ctx: RunContext, img, target: str, assets: list[dict], prompts: dict[str, str],
                      seg: dict) -> dict:
    """自动核验 SAM 结果（替代人工确认）：原提示词 + 简化说法都去分割，候选实例抠图后用 SigLIP-2
    与完整描述做图文匹配，选最像的那组；全都不像则不给实例（退回 LLM 框选并标记不可靠）。"""
    from PIL import Image
    W, H = img.size
    tmp = ctx.dir / "concept" / "_scene" / "sam_verify"
    tmp.mkdir(parents=True, exist_ok=True)
    used: list[list[float]] = []  # 已被其他子资产选作代表的实例框：同一实例不能代表两类资产
    for a in assets:
        pr = prompts[a["id"]]
        words = [w for w in pr.split() if w.lower() not in STOP_WORDS]
        alts = [str(x).strip() for x in (a.get("sam_alts") or []) if str(x).strip()][:4]
        variants = [pr] + alts + [" ".join(words[k:]) for k in range(1, len(words))] + [w for w in words if len(w) > 2]
        variants = list(dict.fromkeys(v for v in variants if v))
        extra = [v for v in variants if v not in seg]
        if extra:
            more = await vision.segment_instances(target, extra, ctx.dir / "concept" / "_scene" / "sam" / a["id"])
            seg.update(more or {})
        cands = []  # (变体, 实例)
        for v in variants:
            for x in sorted(seg.get(v) or [], key=lambda x: -_rep_score(x, a["bbox"], W, H))[:4]:
                cands.append((v, x))
        if not cands:
            continue
        paths = []
        for i, (_, x) in enumerate(cands):
            pth = tmp / f"{a['id']}_{i:02d}.png"
            _masked_crop(img, Image.open(x["mask"]), pth)
            paths.append(pth)
        probs = await vision.text_match(paths, [f"a {pr}"])
        if probs is None:  # 核验不可用：保持原提示词的结果
            continue
        dup = lambda i: any(_box_iou(cands[i][1]["box"], u) > 0.6 for u in used)
        best = max(range(len(cands)), key=lambda i: probs[i][0] + 0.05 * _rep_score(cands[i][1], a["bbox"], W, H)
                   - (1.0 if dup(i) else 0.0))
        v, x = cands[best]
        if dup(best):
            ctx.emit("warn", f"{a['name']}：最相符的实例已被其他子资产选用，退回 LLM 框选，标记不可靠")
            seg[pr] = []
            prompts[a["id"]] = pr
            continue
        used.append(x["box"])
        a["sam_verify"] = {"prompt": v, "prob": probs[best][0]}
        if probs[best][0] < 0.01:
            ctx.emit("warn", f"{a['name']}：SAM 候选与 “{pr}” 都不像（最高匹配 {probs[best][0]:.3f}），退回 LLM 框选，标记不可靠")
            seg[pr] = []
            prompts[a["id"]] = pr
            continue
        if v != pr:
            ctx.emit("info", f"{a['name']}：SAM 用 “{v}” 找到的实例与 “{pr}” 最相符（匹配 {probs[best][0]:.2f}）")
        # 该资产的实例 = 所选说法下、与最佳实例匹配度相近的那些
        keep = [c[1] for i, c in enumerate(cands) if c[0] == v and probs[i][0] >= 0.3 * probs[best][0]]
        rest = [y for y in (seg.get(v) or []) if y not in [c[1] for c in cands]]
        key = f"{pr}#verified"
        seg[key] = [x] + [y for y in keep if y is not x] + rest
        prompts[a["id"]] = key
    return seg


def _rep_conflicts(rep_masks: dict[str, str], assets: list[dict], thr: float = 0.5) -> list[tuple[dict, dict]]:
    """代表实例两两比像素重叠（256 px 上算，控制台环境没有 numpy，用 PIL 直方图计数）：重叠 ≥ 较小者 thr 的成对返回。"""
    from PIL import Image, ImageChops
    by = {a["id"]: a for a in assets}
    small = {}
    for k, p in rep_masks.items():
        try:
            small[k] = Image.open(p).convert("L").resize((256, 256)).point(lambda v: 255 if v > 127 else 0)
        except OSError:
            continue
    area = {k: m.histogram()[255] for k, m in small.items()}
    keys = [k for k in small if k in by and area[k] > 0]
    out = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            inter = ImageChops.multiply(small[a], small[b]).histogram()[255]
            if inter >= thr * min(area[a], area[b]):
                out.append((by[a], by[b]))
    return out


def _terrain_rep(insts: list[dict]) -> dict:
    """地形（岛体 / 台地）的代表实例取面积最大的那块：主岛就是画面里最大的陆块。
    按置信度和拆分框挑，会挑中远处一小块岬角（莫里斯岛实测：岛顶草地量到了远处海岸，量成蓝色）。"""
    def area(x):
        b = x["box"]
        return float(x.get("area") or (b[2] - b[0]) * (b[3] - b[1]))
    return max(insts, key=area)


def _rep_score(x: dict, hint: list[float], W: int, H: int) -> float:
    """挑代表实例：置信度 + 与 LLM 框选提示重叠 + 尺寸；被画面边缘截断、或太小（短边 < 64 像素）的强烈降权。"""
    b = x["box"]
    short = min((b[2] - b[0]) * W, (b[3] - b[1]) * H)
    cut = b[0] <= 0.005 or b[1] <= 0.005 or b[2] >= 0.995 or b[3] >= 0.995
    return (x["score"] + 0.5 * _box_iou(b, hint) + 0.3 * min(1.0, short / 160)
            - (0.8 if cut else 0.0) - (1.0 if short < 64 else 0.0))


def _box_iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _masked_crop(img, mask, out: Path, margin: float = 0.08) -> None:
    """只保留掩码内的物体，贴到与渲染相同的灰底，裁到掩码包围盒：子资产的干净目标图。"""
    from PIL import Image
    m = mask.convert("L").resize(img.size).point(lambda v: 255 if v > 127 else 0)
    bb = m.getbbox() or (0, 0, img.width, img.height)
    bg = Image.new("RGB", img.size, (116, 120, 126))
    bg.paste(img, (0, 0), m)
    dx, dy = int((bb[2] - bb[0]) * margin) + 2, int((bb[3] - bb[1]) * margin) + 2
    box = (max(0, bb[0] - dx), max(0, bb[1] - dy), min(img.width, bb[2] + dx), min(img.height, bb[3] + dy))
    bg.crop(box).save(out)
    m.crop(box).save(out.with_name("mask.png"))   # 同框掩码：图生 3D 抠图、Blender 选朝向与投影取色用


def _layout_from_spec(spec: dict) -> list[dict]:
    out = []
    for a in spec["assets"]:
        for k, p in enumerate(a["placements"]):
            out.append({"id": f"{a['id']}_{k + 1}", "asset": a["id"], "at": p.get("at", [0, 0, 0]),
                        "rot_z": p.get("rot_z", 0), "scale": p.get("scale", 1)})
    return out


def _water_level(spec: dict, asset_id: str) -> bool:
    """深度定位测得这个物件立在支撑平面（水面 / 地面 z=0）上：组装时不要再把它落到地形顶面上（码头、船）。"""
    a = next((x for x in spec.get("assets", []) if x["id"] == asset_id), None)
    if not a or (a.get("measured") or {}).get("source") not in ("depth", "depth+box"):
        return False
    return all(abs(float((p.get("at") or [0, 0, 0])[2])) < 0.05 for p in a.get("placements") or [{}])


def _ground_flags(model: dict) -> dict:
    """组装时的落地标记：地形（Oskar 引擎岛体）是落脚面；图生 3D / 蓝图的子资产落到它表面上；远景层不参与。"""
    mode = model.get("mode")
    return {"backdrop": bool(model.get("backdrop")), "terrain": mode == "terrain",
            "ground": mode not in ("terrain", "backdrop", "skip") and not model.get("backdrop")}


def _place_backdrops(spec: dict, layout: list[dict]) -> list[dict]:
    """远景层（云海 / 海面）不能压住近景：沿参考机位的视线方向把它整片推到近景范围之外，顶面压到近景地面以下。
    拆分给的远景摆放常以场景原点为中心、尺寸上百米，直接用会把机位、地形和角色都埋进团块里。"""
    import math
    by = {a["id"]: a for a in spec.get("assets", [])}
    back = {k for k, a in by.items() if a.get("method") == "backdrop" or a.get("kind") == "backdrop"}
    if not back:
        return layout
    cam = spec.get("camera") or {}
    eye = (cam.get("eye") or [0, 0, 0])[:3]
    tgt = (cam.get("target") or [eye[0], eye[1] - 1, eye[2]])[:3]
    dx, dy = tgt[0] - eye[0], tgt[1] - eye[1]
    n = math.hypot(dx, dy) or 1.0
    dx, dy = dx / n, dy / n
    near_far, ground_top = 0.0, 0.0
    pts = [(x.get("at") or [0, 0, 0])[:2] for x in layout if x["asset"] not in back]
    cx = sum(p[0] for p in pts) / len(pts) if pts else 0.0
    cy = sum(p[1] for p in pts) / len(pts) if pts else 0.0
    for x in layout:
        a = by.get(x["asset"])
        if x["asset"] in back or a is None or a.get("method") == "skip":
            continue
        sz = a.get("size_m") or [1, 1, 1]
        sc = float(x.get("scale", 1) or 1)
        at = x.get("at") or [0, 0, 0]
        near_far = max(near_far, (at[0] - eye[0]) * dx + (at[1] - eye[1]) * dy + max(sz[0], sz[1]) * sc / 2)
        if a.get("method") == "terrain" or a.get("kind") == "environment":
            ground_top = max(ground_top, at[2] + sz[2] * sc) if ground_top else at[2] + sz[2] * sc
    out = []
    for x in layout:
        if x["asset"] in back and (by[x["asset"]].get("measured") or {}).get("on_plane"):
            # 深度测量表明这一层就躺在支撑平面上（海面 / 湖面 / 地面）：顶面压在 z=0，以近景中心为圆心，至少原尺寸。
            sz = by[x["asset"]].get("size_m") or [1, 1, 1]
            sc = max(1.0, float(x.get("scale", 1) or 1))
            x = {**x, "at": [round(cx, 2), round(cy, 2), round(-sz[2] * sc, 2)], "scale": sc}
        elif x["asset"] in back:
            sz = by[x["asset"]].get("size_m") or [1, 1, 1]
            sc = float(x.get("scale", 1) or 1)
            d = near_far + 1.0 + max(sz[0], sz[1]) * sc / 2
            x = {**x, "at": [round(eye[0] + dx * d, 2), round(eye[1] + dy * d, 2),
                             round(min(ground_top, eye[2]) - 1.0 - sz[2] * sc, 2)]}
        out.append(x)
    return out


def _num_or(v, default: float, pick: int = -1) -> float:
    """补丁里的数值字段：模型有时写成列表（scale [1,1,1]、rot_z [0,0,30]），取一个数；无法解析就用默认值。"""
    if isinstance(v, (list, tuple)):
        nums = [x for x in v if isinstance(x, (int, float))]
        if not nums:
            return default
        return sum(nums) / len(nums) if pick == 0 else nums[pick]
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm_place(f: dict, base: dict | None = None) -> dict:
    """统一布局字段：at = 3 个数（缺 z 补原值 / 0），rot_z = 一个角度，scale = 一个均匀缩放。"""
    out = {}
    if "at" in f:
        at = f["at"] if isinstance(f["at"], (list, tuple)) else []
        old = list((base or {}).get("at") or [0, 0, 0]) + [0, 0, 0]
        vals = [_num_or(at[i], old[i]) if i < len(at) else old[i] for i in range(3)]
        out["at"] = vals
    if "rot_z" in f:
        out["rot_z"] = _num_or(f["rot_z"], float((base or {}).get("rot_z", 0) or 0))
    if "scale" in f:
        out["scale"] = max(0.05, _num_or(f["scale"], float((base or {}).get("scale", 1) or 1), pick=0))
    return out


def _apply_layout_patch(layout: list[dict], patch: dict, assets: set[str]) -> list[dict]:
    import copy
    lay = copy.deepcopy(layout)
    rm = set(patch.get("remove") or [])
    lay = [x for x in lay if x["id"] not in rm]
    by = {x["id"]: x for x in lay}
    for iid, f in (patch.get("update") or {}).items():
        if iid in by and isinstance(f, dict):
            by[iid].update(_norm_place(f, by[iid]))
    for q in patch.get("add") or []:
        if isinstance(q, dict) and q.get("asset") in assets:
            n = sum(1 for x in lay if x["asset"] == q["asset"]) + 1
            iid = f"{q['asset']}_{n}"
            while iid in by:
                n += 1
                iid = f"{q['asset']}_{n}"
            item = {"id": iid, "asset": q["asset"], "at": [0, 0, 0], "rot_z": 0.0, "scale": 1.0,
                    **_norm_place({k: q[k] for k in ("at", "rot_z", "scale") if k in q})}
            lay.append(item)
            by[iid] = item
    return lay


_REGION_CN = {"terrain": "地形", "terrain_rock": "崖壁", "terrain_top": "岛顶草地", "water": "水面", "sky": "天空"}


def _region_cn(k: str) -> str:
    return _REGION_CN.get(k, k.replace("asset:", ""))


_OWN_FLOWER_KEEP = ("petal", "center")
_OWN_ROCK_KEEP = ("rock",)  # island cliff mats (osk_rock*) — not grass/path


def _weighted_rgb(lst: list) -> list[float]:
    tot = sum(px for px, _ in lst) or 1
    return [round(sum(px * c[i] for px, c in lst) / tot, 4) for i in range(3)]


def _own_colors(root_stats: dict, roots: dict, materials: list | None = None) -> dict:
    """实例像素中位色 → 资产区域的渲染色 {"asset:<id>": [r,g,b]}（同一资产多个实例按像素数加权）。

    若给了 material_stats 的 materials 列表：
    - 对含 petal/center 的花类资产只用花头材质计 own（茎/叶/描边常与草地/树冠 ID 碰撞，会把 own 拉成绿）。
    - 另把 SAM 陆地区域绑到岛地形材质 own：`terrain_rock`←rock 材质、`terrain`←island_terrain
      根 own。崖壁/陆地 SAM 蒙版常混进海天边缘，中位色会偏青，ΔE 虚高（与花茎碰撞同类问题）。
    """
    flower_with_petal: set[str] = set()
    petal_acc: dict[str, list] = {}
    rock_acc: list = []
    if materials:
        for m in materials:
            aid = roots.get(m.get("root"))
            if not aid or not m.get("rendered"):
                continue
            base = (m.get("material") or "").lower()
            px = int(m.get("px", 0))
            if any(k in base for k in _OWN_FLOWER_KEEP):
                petal_acc.setdefault(aid, []).append((px, m["rendered"]))
            if aid == "island_terrain" and any(k in base for k in _OWN_ROCK_KEEP):
                rock_acc.append((px, m["rendered"]))
        flower_with_petal = set(petal_acc)

    acc: dict[str, list] = {}
    for rt, v in root_stats.items():
        aid = roots.get(rt)
        if not aid or not v.get("rendered"):
            continue
        if aid in flower_with_petal:
            continue  # prefer petal rows
        acc.setdefault(aid, []).append((int(v.get("px", 0)), v["rendered"]))
    out: dict = {}
    for aid, lst in acc.items():
        out[f"asset:{aid}"] = _weighted_rgb(lst)
    for aid, lst in petal_acc.items():
        out[f"asset:{aid}"] = _weighted_rgb(lst)
    # Cliff SAM region → rock-mat own (SAM cliff mask often fringes sea/sky → teal median).
    # Do NOT bind plain `terrain` the same way: enabling own flips the target side to
    # dominant_median on a mixed grass/rock/sea mask and can raise ΔE.
    if rock_acc:
        out["terrain_rock"] = _weighted_rgb(rock_acc)
    return out


async def _color_check(ctx: RunContext, spec: dict, target: str, render: str, hub=None, cam: dict | None = None,
                       roots: dict | None = None) -> dict | None:
    """配色验收：同机位渲染图与目标图按配色环节的区域比颜色差（在 envs/depth 子进程里算）。
    给了 hub + cam + roots 时，物件区域的渲染色取该物件自己的像素（材质 ID 图），颜色和摆放分开评。"""
    import subprocess
    regs = ((spec.get("palette_measured") or {}).get("regions")) or {}
    if not regs or not Path(render).exists():
        return None
    d = ctx.dir / "concept" / "_scene" / "depth"
    rj, oj = d / "regions.json", d / f"color_check_{Path(render).stem}.json"
    rj.write_text(json.dumps(regs, ensure_ascii=False))
    extra = []
    if hub is not None and cam and roots:
        st = await hub.call("blender_material_stats", {"image": render, "camera": cam})
        own = _own_colors((st.get("roots") or {}) if st.get("ok") else {}, roots,
                          (st.get("materials") if st.get("ok") else None))
        if own:
            ojn = d / f"own_{Path(render).stem}.json"
            ojn.write_text(json.dumps(own))
            extra = [str(ojn)]
    py = config.ROOT / "envs" / "depth" / "bin" / "python"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    try:
        r = await asyncio.to_thread(subprocess.run, [str(py), str(Path(__file__).with_name("color_palette.py")), "check",
                                                     target, render, str(rj), str(oj), *extra],
                                    cwd=config.ROOT, env=env, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not oj.exists():
            raise RuntimeError((r.stderr or r.stdout)[-300:])
        return json.loads(oj.read_text())
    except Exception as e:  # noqa: BLE001 — 验收失败不阻塞组装
        ctx.emit("warn", f"配色验收没有算出来：{str(e)[:200]}")
        return None


def _srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


# 引擎描边（反向外壳）是线稿，不是表面颜色：渲染里只剩一两像素宽的暗边，按面色去比会被放大几十倍
LINE_MATERIALS = frozenset({"osk_outline"})


def _strip_index(name: str):
    import re as _re
    mm = _re.search(r"_s(\d+)$", name)
    return int(mm.group(1)) if mm else None


def _color_fix_step(materials: list[dict], roots: dict, cur: dict, backdrops: set | frozenset = frozenset(),
                    damping: float = 0.8, min_px: int = 120) -> tuple[dict, dict]:
    """配色闭环的一步：每种材质「设定色 ÷ 渲染色」（线性、逐通道）就是它在这套光照下还差的倍数。
    整体明暗交给曝光闭环：先除掉实体材质亮度比的中位数，只改各材质之间的相对颜色；每步走 damping，系数限 [0.33, 3]。
    返回 (新系数 {资产: {材质: [r,g,b]}}, 摘要)。"""
    lum = (0.2126, 0.7152, 0.0722)
    rows = []
    for m in materials:
        asset = roots.get(m.get("root"))
        if not asset or int(m.get("px", 0)) < min_px or m.get("material") in LINE_MATERIALS:
            continue
        # 远景（海面平板）：目标色取目标图同位置的颜色——分条取色按图像行线性对应，透视下近处海面会拿到远处的浅色
        goal = m["target"] if asset in backdrops and m.get("target") else m["authored"]
        a = [_srgb_to_lin(float(x)) for x in goal[:3]]
        r = [_srgb_to_lin(float(x)) for x in m["rendered"][:3]]
        la, lr = sum(w * x for w, x in zip(lum, a)), sum(w * x for w, x in zip(lum, r))
        if la < 1e-4 or lr < 1e-4:
            continue
        rows.append((asset, m["material"], int(m["px"]), [ai / max(ri, 1e-4) for ai, ri in zip(a, r)], la / lr))
    solid = sorted((g, px) for asset, _, px, _, g in rows if asset not in backdrops)
    if solid:
        half, acc, g = sum(px for _, px in solid) / 2, 0, solid[-1][0]
        for v, px in solid:
            acc += px
            if acc >= half:
                g = v
                break
    else:
        g = 1.0
    acc_ratio: dict[tuple[str, str], list] = {}
    for asset, mat, px, ratio, _ in rows:
        acc_ratio.setdefault((asset, mat), []).append((px, ratio))
    # 远景海面的分条：相邻条的系数做三点平滑，免得一条一条跳成色带
    for b in backdrops:
        keys = [k for k in acc_ratio if k[0] == b]
        if len(keys) < 3 or any(_strip_index(k[1]) is None for k in keys):   # 背景里有不分条的材质：不平滑
            continue
        keys.sort(key=lambda k: _strip_index(k[1]))
        avg = {}
        for i, k in enumerate(keys):
            nb = keys[max(0, i - 1):i + 2]
            vals = [acc_ratio[n] for n in nb]
            tot = sum(px for lst in vals for px, _ in lst)
            avg[k] = [(tot, [sum(px * r[c] for lst in vals for px, r in lst) / tot for c in range(3)])]
        acc_ratio.update(avg)
    new = {k: {m: list(v) for m, v in d.items()} for k, d in (cur or {}).items()}
    worst = []
    for (asset, mat), lst in acc_ratio.items():
        tot = sum(px for px, _ in lst)
        ratio = [sum(px * r[c] for px, r in lst) / tot for c in range(3)]
        rel = [x / g for x in ratio]
        old = new.get(asset, {}).get(mat, [1.0, 1.0, 1.0])
        # 每步最多改一倍、总共限 [0.2, 3]：一步改太猛（比如暗部材质只有零星像素）会把颜色推出界
        f = [round(max(0.2, min(3.0, o * max(0.5, min(2.0, x ** damping)))), 4) for o, x in zip(old, rel)]
        new.setdefault(asset, {})[mat] = f
        worst.append((max(abs(math.log(max(x, 1e-6))) for x in rel), asset, mat))
    worst.sort(reverse=True)
    return new, {"materials": len(acc_ratio), "global": round(g, 3),
                 "largest": [f"{a}/{m} ×{math.exp(v):.2f}" for v, a, m in worst[:4]]}


def _rargs(spec: dict, rec: dict) -> dict:
    """组装阶段所有同机位渲染共用的参数：背景、灯光（实测布光 rig 优先）、曝光、天空盒。"""
    light = spec.get("light_measured") or {}
    a: dict = {"backdrop": spec.get("backdrop")}
    if light:
        a["lighting"] = "day"
    if rec.get("light_rig"):
        a["rig"] = rec["light_rig"]
    elif light.get("fit_r2", 0) >= 0.2:
        a["sun_dir"] = light["dir_world"]
    if rec.get("exposure"):
        a["exposure"] = rec["exposure"]
    if rec.get("sky_dome"):
        a["sky"] = rec["sky_dome"]
    return a


async def _depth_env(ctx: RunContext, script: str, args: list, timeout: int = 300) -> None:
    """在 envs/depth（numpy + PIL）子进程里跑本目录的测量脚本；失败抛 RuntimeError。"""
    import subprocess
    py = config.ROOT / "envs" / "depth" / "bin" / "python"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    r = await asyncio.to_thread(subprocess.run, [str(py), str(Path(__file__).with_name(script)), *map(str, args)],
                                cwd=config.ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-400:])


def _pitch_down(cam: dict) -> float:
    e, t = cam["eye"], cam["target"]
    d = [b - a for a, b in zip(e, t)]
    return math.degrees(math.atan2(-d[2], math.hypot(d[0], d[1])))


async def _sky_dome(ctx: RunContext, spec: dict, rec: dict, target: str, cam: dict) -> None:
    """天空盒（实测）：目标图天空 / 水面每一行的颜色按相机换成仰角，铺成 2:1 全景；相机背景与远海都用它。"""
    regs = ((spec.get("palette_measured") or {}).get("regions")) or {}
    if not (regs.get("sky") or regs.get("water")):
        return
    d = ctx.dir / "concept" / "_scene" / "env"
    d.mkdir(parents=True, exist_ok=True)
    req = {"target": target, "regions": {k: regs[k] for k in ("sky", "water") if regs.get(k)},
           "camera": {"fov_deg": cam.get("fov_deg", 50), "aspect": cam.get("aspect", 1.0), "pitch_down_deg": _pitch_down(cam)},
           "out_png": str(d / "sky_dome.png"), "out_hdr": str(d / "sky_dome.hdr")}
    (d / "dome_req.json").write_text(json.dumps(req, ensure_ascii=False))
    try:
        await _depth_env(ctx, "envgen.py", ["dome", d / "dome_req.json", d / "dome.json"])
        info = json.loads((d / "dome.json").read_text())
    except Exception as e:  # noqa: BLE001 — 天空盒失败就沿用纯色背景
        ctx.emit("warn", f"天空盒没有做出来，沿用纯色背景：{str(e)[:200]}")
        return
    rec["sky_dome"] = info["png"]
    rec["sky_dome_hdr"] = info.get("hdr")
    ctx.artifact("decompose", info["png"], "天空盒（目标图实测的天空 / 远海渐变，2:1 全景）")
    ctx.emit("info", f"天空盒：按目标图 {info['sky_rows']} 档仰角的天色、{info['sea_rows']} 档俯角的海色铺成全景"
                     f"（天 {info.get('sky_range_deg')}°、海 {info.get('sea_range_deg')}°）", info)


def _vlin(rgb) -> list[float]:
    return [_srgb_to_lin(float(x)) for x in rgb[:3]]


def _vlum(c) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _rig_init(spec: dict, lt: dict) -> dict:
    """实测布光的初值：主光方向取光照拟合的方向；主光色 = 天光色 × 亮暗比 k 的色相；环境（世界）色取实测天色。"""
    light = spec.get("light_measured") or {}
    sky_hex = (((spec.get("palette_measured") or {}).get("sky") or {}).get("base")) or "#A0C8F0"
    sky = [_srgb_to_lin(int(sky_hex[i:i + 2], 16) / 255) for i in (1, 3, 5)]
    kt = lt["k"]
    key = [a * max(k, 0.05) for a, k in zip(sky, kt)]
    mk = max(key) or 1.0
    amb = [c * 0.7 / max(_vlum(sky), 1e-3) for c in sky]
    return {"key": {"dir": light.get("dir_world") or [0.4, -0.5, 0.75], "color": [round(c / mk, 4) for c in key],
                    "energy": 3.3, "angle_deg": 2.0},
            "fill": {"energy": 0.2, "color": [round(c / max(amb), 4) for c in amb]},
            "rim": {"energy": 0.3},
            "world": {"color": [round(c, 4) for c in amb], "strength": 0.75}}


def _vlum(c) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _rig_step(rig: dict, lt: dict, lr: dict, damping: float = 0.8) -> tuple[dict, float]:
    """布光闭环的一步（lt / lr：目标图 / 渲染图的 lambert_fit）。主光强度按亮暗比 k 的亮度对齐，主光色按 k 的色相对齐，
    环境色按 a（≈ 本色 × 环境光）的色相对齐。返回 (新 rig, 误差)。"""
    import copy
    kt, kr = lt["k"], lr["k"]
    lkt, lkr = max(_vlum(kt), 1e-3), max(_vlum(kr), 0.02)
    q = lkt / lkr
    hue_k = [max(0.7, min(1.4, (a / lkt) / max(b / lkr, 1e-3))) if b > 1e-3 else 1.0 for a, b in zip(kt, kr)]
    at, ar = lt["a"], lr["a"]
    lat, lar = max(_vlum(at), 1e-4), max(_vlum(ar), 1e-4)
    hue_a = [max(0.7, min(1.4, (x / lat) / max(y / lar, 1e-4))) for x, y in zip(at, ar)]
    err = abs(math.log(q)) + sum(abs(math.log(x)) for x in hue_k) / 3 + sum(abs(math.log(x)) for x in hue_a) / 3
    new = copy.deepcopy(rig)
    f = max(0.4, min(2.5, q ** damping))
    e = new["key"]["energy"] * f
    if e > 12.0:   # 主光到顶了：把剩下的比例交给环境光（压暗环境同样拉开亮暗比）
        new["world"]["strength"] = round(max(0.1, new["world"]["strength"] * 12.0 / e), 4)
        e = 12.0
    new["key"]["energy"] = round(max(0.3, e), 4)
    kc = [c * (h ** damping) for c, h in zip(new["key"]["color"], hue_k)]
    new["key"]["color"] = [round(c / max(kc), 4) for c in kc]
    wc = [c * (h ** damping) for c, h in zip(new["world"]["color"], hue_a)]
    lw = _vlum(new["world"]["color"]) / max(_vlum(wc), 1e-4)
    new["world"]["color"] = [round(c * lw, 4) for c in wc]
    return new, round(err, 4)


async def _light_loop(ctx: RunContext, hub, spec: dict, rec: dict, target: str, cam: dict) -> None:
    """布光闭环：目标图上（深度模型法线）拟合「颜色 ≈ 环境 + 主光·max(0, 法线·光向)」得到亮暗比 k 与环境色；
    按它搭灯，渲同机位图 + 法线通道，在渲染图上用渲染法线拟合同一个量，误差降了才保留，最多 light_fix_max_steps 步。
    与几何无关：两边的崖面分块不必一一对应。之后所有组装渲染都用这套灯。"""
    lt = spec.get("light_fit")
    npz = next((p for p in (ctx.dir / "concept" / "_scene" / "depth" / n for n in ("depth_da3_fused.npz", "depth_moge2.npz",
                                                                                   "depth_da3.npz")) if p.exists()), None)
    regs = ((spec.get("palette_measured") or {}).get("regions")) or {}
    mask = regs.get("terrain_rock") or regs.get("terrain")
    d = ctx.dir / "concept" / "_scene" / "env"
    d.mkdir(parents=True, exist_ok=True)
    if not lt and npz is not None and mask:
        try:
            await _depth_env(ctx, "color_palette.py", ["lighttarget", target, npz, mask, d / "light_target.json"])
            lt = json.loads((d / "light_target.json").read_text())
        except Exception as e:  # noqa: BLE001
            ctx.emit("warn", f"布光：目标图亮暗比测量失败，沿用日光预设：{str(e)[:200]}")
            lt = None
        if lt:
            spec["light_fit"] = lt
    if not lt:
        ctx.emit("info", "布光：目标图岩面上测不出可靠的亮暗关系（法线变化或可用像素不够），沿用日光预设")
        return
    (d / "light_lc.json").write_text(json.dumps(lt["lc"]))
    rig = _rig_init(spec, lt)
    best = None
    for step in range(int(ctx.opt("light_fix_max_steps", 4)) + 1):
        rec["light_rig"] = rig
        # 留曝光余量：亮暗比与曝光无关，但过曝会把亮面顶平；按当前曝光再压 light_headroom 档渲测光图
        rargs = _rargs(spec, rec)
        rargs["exposure"] = round(min(float(rec.get("exposure") or 0.0), 0.0) - float(ctx.opt("light_headroom", 0.5)), 3)
        r = await hub.call("blender_render", {**rargs, "views": ["ref"], "label": f"light_{step}", "camera": cam})
        img = (r.get("images") or [None])[0]
        if not img:
            break
        from PIL import Image as _Im
        with _Im.open(img) as im:
            wh = list(im.size)
        npass = await hub.call("blender_normal_pass", {"camera": cam, "size": wh, "label": f"light_{step}"})
        if not npass.get("ok"):
            ctx.emit("warn", f"布光：法线通道渲染失败：{str(npass.get('error'))[:200]}")
            break
        try:
            await _depth_env(ctx, "color_palette.py", ["lightrender", img, npass["path"], mask, d / "light_lc.json",
                                                      d / f"light_render_{step}.json"], timeout=120)
            lr = json.loads((d / f"light_render_{step}.json").read_text())
        except Exception as e:  # noqa: BLE001
            ctx.emit("warn", f"布光：渲染图亮暗比测量失败：{str(e)[:200]}")
            break
        if not lr:
            break
        new, err = _rig_step(rig, lt, lr)
        ctx.emit("info", f"布光闭环 第 {step} 步：亮暗比 k 渲染 {lr['k']} / 目标 {lt['k']}，误差 {err}；"
                         f"主光 {rig['key']['energy']}（色 {rig['key']['color']}）、环境 {rig['world']['strength']}",
                 {"rig": rig, "render_fit": lr, "err": err})
        if best is None or err < best[1]:
            best = (rig, err)
        if err < 0.08:
            break
        rig = new
    if best:
        rec["light_rig"] = best[0]
        rec["light_err"] = best[1]


def _orbit_camera(cam: dict, down_deg: float) -> dict:
    """绕注视点把相机俯角再加 down_deg 度（距离、视角、水平朝向不变）。"""
    ex, ey, ez = (float(v) for v in cam["eye"])
    tx, ty, tz = (float(v) for v in cam["target"])
    vx, vy, vz = ex - tx, ey - ty, ez - tz
    hd = math.hypot(vx, vy)
    dist = math.hypot(hd, vz)
    if hd < 1e-6 or dist < 1e-6:
        return dict(cam)
    el = max(-10.0, min(80.0, math.degrees(math.atan2(vz, hd)) + down_deg))
    nh, nz = dist * math.cos(math.radians(el)), dist * math.sin(math.radians(el))
    return {**cam, "eye": [round(tx + vx / hd * nh, 3), round(ty + vy / hd * nh, 3), round(tz + nz, 3)]}


async def _fit_camera_pitch(ctx: RunContext, hub, spec: dict, target: str, render_args: dict) -> dict | None:
    """相机俯角闭环：深度模型对风格化图的俯角常估得偏平（地平线被画师压低）。
    以实测相机为起点，绕注视点试几个俯角，各渲一张同机位图，按客观相似度（DINO + SigLIP）取最好的。"""
    base = spec.get("camera_measured") or spec.get("camera")
    if not base:
        return None
    async def score(offsets):
        imgs = []
        for d in offsets:
            r = await hub.call("blender_render", {**render_args, "views": ["ref"], "label": f"camfit_{d:+g}",
                                                  "camera": _orbit_camera(base, d)})
            imgs.append((r.get("images") or [None])[0])
        ok = [(d, i) for d, i in zip(offsets, imgs) if i]
        if not ok:
            return {}
        m = await vision.objective_similarity(target, [i for _, i in ok])
        if not m:
            return {}
        return {d: vision.objective_index({"dino": m["dino_per"][k], "siglip": m["siglip_per"][k]})
                for k, (d, _) in enumerate(ok)}
    tried = await score([-6, 0, 6, 12, 18, 24, 30])   # 负值 = 比实测更平（仰）
    if not tried:
        return None
    best = max(tried, key=tried.get)
    tried.update(await score([d for d in (best - 3, best + 3) if d >= -9 and d not in tried]))
    best = max(tried, key=tried.get)
    gain = tried[best] - tried[0]
    use = best if best != 0 and gain >= 1.0 else 0   # 至少高出一个轮间波动单位才换机位
    spec["camera_measured"] = base
    spec["camera"] = _orbit_camera(base, use)
    return {"tried": {f"{k:+g}": round(v, 2) for k, v in sorted(tried.items())}, "chosen_deg": use,
            "gain": round(gain, 2) if use else 0.0}


def _regions_worse(before: dict, after: dict, region_assets: dict, tol: float = 2.0) -> set:
    """配色验收的区域里，色差比上一步变差超过 tol 的，对应到哪些资产（asset:<id> → 该资产；terrain* → 地形资产；
    water / sky → 背景资产）。"""
    out = set()
    for k, v in after.items():
        b = before.get(k)
        if not b or float(v["delta_e"]) <= float(b["delta_e"]) + tol:
            continue
        if k.startswith("asset:"):
            out.add(k.split(":", 1)[1])
        else:
            out |= set(region_assets.get(k.split("_")[0], ()))
    return out


def _hex_rgb(h: str) -> list[float]:
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def _asset_level_fix(cur: dict, fails: dict, mat_names: dict, damping: float = 0.8) -> dict:
    """物件整体调色：每个不合格物件的全部材质乘同一个系数 = (目标区域色 ÷ 物件自身像素色)^damping（线性、逐通道，限 [0.5, 2]）。"""
    new = {k: {m: list(v) for m, v in d.items()} for k, d in (cur or {}).items()}
    for aid, v in fails.items():
        t = [_srgb_to_lin(x) for x in _hex_rgb(v["target"])]
        r = [_srgb_to_lin(x) for x in _hex_rgb(v["render"])]
        f = [max(0.5, min(2.0, (a / max(b, 1e-4)) ** damping)) for a, b in zip(t, r)]
        for m in mat_names.get(aid, ()):
            old = new.setdefault(aid, {}).get(m, [1.0, 1.0, 1.0])
            new[aid][m] = [round(max(0.2, min(3.0, o * x)), 4) for o, x in zip(old, f)]
    return new



def _color_fix_dino_ok(de: float, best_de: float, dino, best_dino, *,
                       dino_tol: float = 0.01, dino_tol_when_de: float = 0.05,
                       de_min: float = 0.2) -> bool:
    """配色闭环 DINO 门：ΔE 必须变好；大幅降 ΔE 时允许更大 DINO 回落；基线本就不像时不挡颜色。"""
    improve = float(best_de) - float(de)
    if improve < de_min:
        return False
    if dino is None or best_dino is None:
        return True
    tol = float(dino_tol)
    if improve >= 1.0:
        tol = max(tol, float(dino_tol_when_de))
    # 场景整体已经不像（常见冷启动 / 布局差）：颜色仍应能落地，只要 ΔE 有实质改善
    if float(best_dino) < 0.35 and improve >= 0.5:
        return True
    return float(dino) >= float(best_dino) - tol


async def _color_fix_loop(ctx: RunContext, hub, spec: dict, rec: dict, target: str, cam: dict, roots: dict,
                          rargs: dict) -> None:
    """配色闭环（在同一个布局上做，不和摆放变化混在一起）：渲同机位图 → 配色验收 → 按材质测渲染色 → 改基色 → 再渲再验收；
    平均 ΔE 降了才保留这一步，没降就退回上一套并停止。最多 color_fix_max_steps 步。"""
    r = await hub.call("blender_render", {**rargs, "views": ["ref"], "label": "colorfix_0", "camera": cam})
    img = (r.get("images") or [None])[0]
    c = await _color_check(ctx, spec, target, img, hub, cam, roots) if img else None
    if not c:
        return
    best_de, best_fix = float(c["mean_delta_e"]), rec.get("color_fix") or {}
    backdrops = {a["id"] for a in spec["assets"]
                 if ((ctx.state.get("models") or {}).get(a["id"]) or {}).get("mode") == "backdrop"}
    start_de, steps = best_de, 0
    mat_names: dict[str, set] = {}
    prev_regions = c.get("regions") or {}
    water_mask = ((spec.get("palette_measured") or {}).get("regions") or {}).get("water") or ""
    # 颜色改动不能让画面整体更不像：每步还要看客观相似度（DINO），掉超过 color_fix_dino_tol（默认 0.01，约等于轮间波动）就不收
    m0 = await vision.objective_similarity(target, [img])
    best_dino = (m0 or {}).get("dino")
    models = ctx.state.get("models") or {}
    region_assets = {"terrain": {a["id"] for a in spec["assets"] if (models.get(a["id"]) or {}).get("mode") == "terrain"},
                     "water": backdrops, "sky": backdrops}
    for step in range(1, int(ctx.opt("color_fix_max_steps", 3)) + 1):
        st = await hub.call("blender_material_stats", {"image": img, "camera": cam, "target": target,
                                                       "target_mask": water_mask})
        if not st.get("ok"):
            ctx.emit("warn", f"配色闭环：按材质测色失败：{str(st.get('error'))[:200]}")
            break
        for m in st.get("materials") or []:
            if roots.get(m.get("root")) and m.get("material") not in LINE_MATERIALS:
                mat_names.setdefault(roots[m["root"]], set()).add(m["material"])
        new, info = _color_fix_step(st.get("materials") or [], roots, best_fix, backdrops)
        try:   # 留底：每步按材质测得的设定色 / 渲染色与新系数，便于复查
            dd = ctx.dir / "concept" / "_scene" / "depth"
            dd.mkdir(parents=True, exist_ok=True)
            (dd / f"color_fix_step{step}.json").write_text(json.dumps(
                {"materials": st.get("materials"), "fix": new, "info": info}, ensure_ascii=False, indent=1))
        except OSError:
            pass
        await hub.call("blender_recolor", {"fixes": new, "roots": roots})
        r = await hub.call("blender_render", {**rargs, "views": ["ref"], "label": f"colorfix_{step}", "camera": cam})
        nimg = (r.get("images") or [None])[0]
        c = await _color_check(ctx, spec, target, nimg, hub, cam, roots) if nimg else None
        de = float(c["mean_delta_e"]) if c else 999.0
        worse = _regions_worse(prev_regions, (c or {}).get("regions") or {}, region_assets)
        if c and worse:
            # 平均降了不代表每块都好：变差的区域，把它对应资产的系数退回上一步，再渲一次复核
            for aid in worse:
                if aid in best_fix:
                    new[aid] = best_fix[aid]
                else:
                    new.pop(aid, None)
            await hub.call("blender_recolor", {"fixes": new, "roots": roots})
            r = await hub.call("blender_render", {**rargs, "views": ["ref"], "label": f"colorfix_{step}b", "camera": cam})
            nimg = (r.get("images") or [None])[0]
            c = await _color_check(ctx, spec, target, nimg, hub, cam, roots) if nimg else None
            de = float(c["mean_delta_e"]) if c else 999.0
            info["reverted"] = sorted(worse)
        mc = await vision.objective_similarity(target, [nimg]) if nimg and best_dino is not None else None
        dino = (mc or {}).get("dino")
        info["dino"] = dino
        ok = _color_fix_dino_ok(
            de, best_de, dino, best_dino,
            dino_tol=float(ctx.opt("color_fix_dino_tol", 0.01)),
            dino_tol_when_de=float(ctx.opt("color_fix_dino_tol_when_de", 0.05)),
            de_min=0.2)
        ctx.emit("info", f"配色闭环 第 {step} 步：按 {info['materials']} 种材质的「设定色 ÷ 渲染色」调基色"
                         f"（整体明暗 ×{info['global']} 交给曝光）；平均 ΔE {best_de} → {de}，"
                         + ("保留" if ok else "没有变好，退回上一套并停止")
                         + (f"（变差的区域已退回：{'、'.join(info['reverted'])}）" if info.get("reverted") else "")
                         + (f"；DINO {dino:.3f}" if dino is not None else "")
                         + f"；改动最大：{'、'.join(info['largest']) or '无'}", {**info, "delta_e": de})
        if not ok:
            await hub.call("blender_recolor", {"fixes": best_fix, "roots": roots})
            break
        best_de, best_fix, img, steps = de, new, nimg, step
        prev_regions = c["regions"]
        if dino is not None:
            best_dino = max(best_dino or 0.0, dino) if best_dino is not None else dino
    # 第二层：材质都已渲成设定色，但物件整体仍和目标图差得远——说明设定色本身取偏了（框选取色带进了水面等）。
    # 对这些物件，按「目标区域色 ÷ 物件自身像素色」整体再乘一次；同样只在验收变好时保留
    for k in range(int(ctx.opt("color_fix_asset_steps", 2))):
        fails = {key.split(":", 1)[1]: v for key, v in prev_regions.items()
                 if key.startswith("asset:") and v.get("own") and not v.get("pass")}
        fails = {aid: v for aid, v in fails.items() if mat_names.get(aid)}
        if not fails:
            break
        new = _asset_level_fix(best_fix, fails, mat_names)
        await hub.call("blender_recolor", {"fixes": new, "roots": roots})
        r = await hub.call("blender_render", {**rargs, "views": ["ref"], "label": f"colorfix_a{k + 1}", "camera": cam})
        nimg = (r.get("images") or [None])[0]
        c = await _color_check(ctx, spec, target, nimg, hub, cam, roots) if nimg else None
        de = float(c["mean_delta_e"]) if c else 999.0
        better = {aid for aid in fails if c and float((c["regions"].get(f"asset:{aid}") or {}).get("delta_e", 999))
                  < float(fails[aid]["delta_e"]) - 1.0}
        mc = await vision.objective_similarity(target, [nimg]) if nimg and best_dino is not None else None
        dino = (mc or {}).get("dino")
        # 平均 ΔE 允许微升（+0.2）；DINO 门按「至少 0.5 的等效改善」评估，基线不像或大幅降 ΔE 时放宽
        ok = bool(better) and de < best_de + 0.2 and _color_fix_dino_ok(
            min(de, best_de - 0.5), best_de, dino, best_dino,
            dino_tol=float(ctx.opt("color_fix_dino_tol", 0.01)),
            dino_tol_when_de=float(ctx.opt("color_fix_dino_tol_when_de", 0.05)),
            de_min=0.5)
        ctx.emit("info", f"配色闭环（整体）第 {k + 1} 步：{'、'.join(sorted(fails))} 按「目标区域色 ÷ 自身像素色」整体调色；"
                         f"平均 ΔE {best_de} → {de}" + (f"、DINO {dino:.3f}" if dino is not None else "") + "；"
                         + (f"变好：{'、'.join(sorted(better))}，保留" if ok else "没有变好（或画面相似度下降），退回"))
        if not ok:
            await hub.call("blender_recolor", {"fixes": best_fix, "roots": roots})
            break
        keep = {aid: v for aid, v in new.items() if aid not in fails or aid in better}
        for aid in fails:
            if aid not in better and aid in best_fix:
                keep[aid] = best_fix[aid]
        best_fix = keep
        if keep != new:
            await hub.call("blender_recolor", {"fixes": best_fix, "roots": roots})
        best_de, prev_regions = min(best_de, de), c["regions"]
        if dino is not None and best_dino is not None:
            best_dino = max(best_dino, dino)
    rec["color_fix"] = best_fix
    rec["color_fix_best"] = {"fix": best_fix, "de": best_de, "from": start_de, "steps": steps}
    rec["color_fix_done"] = True


def _body_size(comp: dict, layout: list) -> list | None:
    """场景主体的包围盒尺寸（米）：去掉远景层（海天背景）——它铺得很大，算进总尺寸会让评审误以为整体放大了几倍。"""
    back = {x["id"] for x in layout if x.get("backdrop")}
    boxes = [b for k, b in (comp.get("instances") or {}).items() if k not in back and b.get("min") and b.get("max")]
    if not boxes:
        return None
    lo = [min(b["min"][i] for b in boxes) for i in range(3)]
    hi = [max(b["max"][i] for b in boxes) for i in range(3)]
    return [round(h - l, 2) for l, h in zip(lo, hi)]


async def stage_compose(ctx: RunContext) -> None:
    spec = ctx.state["spec"]
    target = ctx.state.get("scene_target") or _target_image(ctx, "_scene")
    models = ctx.state.get("models", {})
    glbs = {a["id"]: models.get(a["id"], {}).get("glb") for a in spec["assets"]
            if models.get(a["id"], {}).get("mode") != "skip"}
    missing = [k for k, v in glbs.items() if not v]
    if missing:
        raise RuntimeError(f"子资产尚未导出 GLB：{missing}")
    rec = ctx.state.setdefault("compose", {"rounds": [], "champion": None})
    if any(x["asset"] not in glbs for r in rec["rounds"] for x in r.get("layout", [])):
        ctx.emit("info", "旧的组装记录引用了已不存在的子资产（拆分方案变了），从头组装")
        rec = ctx.state["compose"] = {"rounds": [], "champion": None}
    max_rounds = scene3d_limits(ctx.options)["compose_rounds"] if ctx.run.get("mode") == "scene3d" \
        else int(ctx.opt("compose_max_rounds", 8))
    pass_score = float(ctx.opt("model_pass_score", 7.5))
    jev_dir = ctx.dir / "jev"
    jev_dir.mkdir(exist_ok=True)
    history: list[dict] = rec.setdefault("history", [])
    champ = rec["rounds"][rec["champion"]] if rec["rounds"] and rec["champion"] is not None else None
    layout = _place_backdrops(spec, [x for x in (champ["layout"] if champ else _layout_from_spec(spec)) if x["asset"] in glbs])
    last = champ
    subject = f"场景《{spec.get('title', '')}》：{spec.get('scene', {}).get('description', '')}"
    size = "×".join(f"{v:g}" for v in spec.get("scene", {}).get("size_m", [0, 0, 0]))
    start = max((r["round"] for r in rec["rounds"]), default=0) + 1
    # 已有冠军、但还没做过环境与布光（新环节上线前的组装）：在冠军布局上原样补做，额外给轮数，不先改布局
    env_redo = bool(rec["rounds"]) and not rec.get("env_done") and spec.get("camera") \
        and (ctx.opt("sky_dome", True) or ctx.opt("light_fix", True))
    if env_redo:
        max_rounds = max(max_rounds, start - 1 + int(ctx.opt("env_redo_rounds", 2)))
        ctx.emit("info", f"冠军布局（第 {champ['round']} 轮）补做天空盒与布光闭环，再按新光照重新调色")
    async with MCPHub(ctx.dir, servers=["blender"]) as hub:
        for rnd in range(start, max_rounds + 1):
            await ctx.control.checkpoint()
            ctx.emit("round", f"场景组装 · 第 {rnd}/{max_rounds} 轮", {"round": rnd})
            t0 = time.time()
            if last is not None and last.get("decision") and not (env_redo and rnd == start):
                dec = last["decision"]
                by_id = {d["id"]: d for d in last["observation"]["diffs"]}
                focus = "\n".join(f"- {i} [{by_id[i]['part']}] {by_id[i]['observation']}" for i in dec["focus"] if i in by_id)
                inst = json.dumps(last.get("compose", {}).get("instances", {}), ensure_ascii=False)[:5000]
                msgs = [{"role": "system", "content": "你是场景布局师，只调整已建好子资产的摆放（位置/朝向/缩放/数量）。只输出 JSON。"},
                        user_message(prompts.LAYOUT_REVISE.format(
                            title=spec.get("title", ""), size=size, round=last["round"], score=f"{last['score']:.1f}",
                            choice=dec["next_step"], reason=dec.get("gate_reason", ""), focus=focus or "- 无",
                            plan=dec.get("plan") or "按焦点差异调整摆放",
                            assets="\n".join(f"- {a['id']}（{a['name']}，单个尺寸 {a['size_m']} 米）" for a in spec["assets"])
                            + f"\n实例包围盒：{inst}",
                            layout=json.dumps(layout, ensure_ascii=False)), [last["observation"]["compare"]], max_side=1536)]
                try:
                    patch = await LLM(route(ctx, "patch")).json(msgs, on_delta=ctx.delta_emitter(), thinking_budget=4096, retries=2,
                                           validate=lambda o: None if isinstance(o, dict) and any(
                                               o.get(k) for k in ("update", "add", "remove")) else "需要非空布局补丁")
                    layout = _place_backdrops(spec, _apply_layout_patch(layout, patch, set(glbs)))
                    ctx.emit("info", f"布局补丁：修改 {len(patch.get('update') or {})}、新增 {len(patch.get('add') or [])}、"
                             f"删除 {len(patch.get('remove') or [])} — {patch.get('notes', '')}", patch)
                except Exception as e:
                    ctx.emit("warn", f"布局补丁失败，跳过本轮：{str(e)[:200]}")
                    continue
            _orep = None
            if not (env_redo and rnd == start):
                # 物件层能力（系统工坊 object：间距松弛 / 同类尺寸变化 / 朝向变化）：Router 选 → 副本上执行 → 工坊验证 → 不 fail 才用
                layout, _orep = await CAPS.object_pass(ctx, spec, layout, ((last or {}).get("compose") or {}).get("instances"))
            items = [{**x, "glb": glbs[x["asset"]], **_ground_flags(models.get(x["asset"], {})),
                      **({"ground": False} if _water_level(spec, x["asset"]) else {})}
                     for x in layout]
            comp = await hub.call("blender_compose_scene", {"items": items})
            if not comp.get("ok"):
                raise RuntimeError(f"场景组装失败：{comp.get('error')}")
            roots = {x["id"]: x["asset"] for x in items}
            if rec.get("color_fix"):
                await hub.call("blender_recolor", {"fixes": rec["color_fix"], "roots": roots})
            cam = spec.get("camera")
            if cam and not rec.get("env_done") and rec["rounds"] and rec.get("color_fix_done"):
                # 已有冠军布局时补做环境与布光：之后的调色按新光照重新闭环
                rec.pop("color_fix_done", None)
                rec.pop("color_fix", None)
                rec.pop("color_fix_best", None)
                rec.pop("gscene_done", None)
                await hub.call("blender_recolor", {"fixes": {}, "roots": roots})
            # 本管线的能力按计划执行（默认 实测天空盒 → 布光闭环 → 相机俯角闭环 → 配色闭环），每步 match → run → verify
            sub_ = SimpleNamespace(ctx=ctx, hub=hub, spec=spec, rec=rec, target=target, cam=cam, roots=roots)
            env_was_done = rec.get("env_done")
            await CAPS.run_native_plan(ctx, sub_, tuple(ctx.opt("compose_plan", None) or CAPS.COMPOSE_PLAN))
            cam = sub_.cam
            if cam and not env_was_done:
                rec["env_done"] = True
            if cam and rec.get("color_fix_done") and not rec.get("gscene_done"):
                # 通用场景层能力（系统工坊 gscene：主色归并 / 整体色调 / 风格包）：Router 选中才跑，比对不变差才并进配色
                rec["gscene"] = await CAPS.gscene_pass(ctx, sub_)
                rec["gscene_done"] = True
            # 日光预设 + 实测布光（rig）+ 天空盒 + 曝光
            r = await hub.call("blender_render", {"views": (["ref"] if cam else []) + ["persp_left", "front", "persp"],
                                                  "label": f"scene_r{rnd}", "camera": cam, **_rargs(spec, rec)})
            renders = r.get("images", [])
            facts = (f"{len(layout)} 个实例，主体总尺寸 {_body_size(comp, items) or comp.get('size')} 米"
                     f"（目标约 {spec.get('scene', {}).get('size_m')}；海天远景层不计入）")
            color = await _color_check(ctx, spec, target, renders[0], hub, cam, roots) if cam and renders else None
            if color and abs(color.get("exposure_stops") or 0) >= 0.15:
                # 曝光闭环：下一轮渲染按实测亮度比补曝光（每轮补 80%，累计限 ±2 档）
                rec["exposure"] = round(max(-2.0, min(2.0, (rec.get("exposure") or 0.0) + 0.8 * color["exposure_stops"])), 3)
                ctx.emit("info", f"曝光校正：渲染比目标图{'暗' if color['exposure_stops'] > 0 else '亮'} "
                                 f"{abs(color['exposure_stops']):.2f} 档，下一轮曝光 {rec['exposure']:+.2f} 档")
            if color:
                bad = [f"{_region_cn(k)} ΔE {v['delta_e']}（目标 {v['target']} / 渲染 {v['render']}）"
                       for k, v in color["regions"].items() if not v["pass"]]
                facts += f"；配色验收（同机位分区域色差，≤{color['threshold']:g} 合格）：" + \
                    ("全部合格" if not bad else "；".join(bad))
                ctx.emit("review", f"配色验收 第 {rnd} 轮：平均 ΔE {color['mean_delta_e']}，配色分 {color['color_score']}/10，"
                         + ("全部区域合格" if not bad else f"{len(bad)} 个区域不合格：" + "；".join(bad[:4])), color)
            obs = await jev.observe(target=target, renders=renders, subject=subject,
                                    compare_path=jev_dir / f"scene_r{rnd:02d}_compare.jpg", facts=facts,
                                    style_context=style_agents.rules_text(ctx.options.get("style_profile"), "review") + consultation.revision_rules(ctx.options),
                                    model_key=_reviewer(ctx), on_delta=ctx.delta_emitter())
            checks = [{"name": "全部实例放置成功", "required": True, "passed": not comp.get("errors"),
                       "category": "missing"}]
            if color:
                checks.append({"name": "配色验收（同机位分区域色差）", "required": False, "passed": color["pass"],
                               "category": "color", "detail": f"平均 ΔE {color['mean_delta_e']}"})
            objm = await vision.objective_similarity(target, renders)
            obj = vision.objective_index(objm)
            verdict = None
            if last is not None:
                verdict = await jev.verify_pair(target=target, prev_render=last["renders"][0], cur_render=renders[0],
                                                focus=[d for d in last["observation"]["diffs"]
                                                       if d["id"] in (last.get("decision") or {}).get("focus", [])],
                                                out=jev_dir / f"scene_r{rnd:02d}_pair.jpg",
                                                model_key=route(ctx, "verify"), on_delta=ctx.delta_emitter())
                history.append({"round": rnd, "choice": last["decision"]["next_step"], "focus_text": [],
                                "score_before": last["score"], "score_after": obs["score"],
                                "result": _improved(obj, vision.objective_index(last.get("metrics")), verdict,
                                                    obs["score"], last["score"])})
            passed = jev.ledger_pass(obs["diffs"], pass_score) and all(c["passed"] for c in checks)
            decision = None if passed else await jev.decide(subject=subject, observation=obs, checks=checks,
                                                            history=history, model_key=route(ctx, "decide"),
                                                            on_delta=ctx.delta_emitter())
            round_rec = {"round": rnd, "score": obs["score"], "layout": layout, "renders": renders,
                         "observation": obs, "decision": decision, "verdict": verdict, "checks": checks,
                         "metrics": objm, "objective": obj,
                         "compose": {k: comp.get(k) for k in ("instances", "size", "triangles", "errors")},
                         "seconds": round(time.time() - t0)}
            round_rec["color"] = color
            round_rec["capabilities"] = {"object": _orep}
            rec["rounds"].append(round_rec)
            last_obj = vision.objective_index(last.get("metrics")) if last else None
            if last is None:
                won = True
            elif obj is not None and last_obj is not None and abs(obj - last_obj) > OBJ_TIE:
                won = obj > last_obj  # 台账分在复杂场景里常饱和到 0，没有梯度：以客观相似度为准
            else:
                won = verdict is not None and jev.accept_b(verdict)
            if won:
                rec["champion"] = len(rec["rounds"]) - 1
            ctx.artifact("compare", obs["compare"], f"场景组装 第{rnd}轮 对比", asset="_scene", round=rnd, score=obs["score"])
            for p in renders:
                ctx.artifact("render", p, f"场景组装 第{rnd}轮", asset="_scene", round=rnd, score=obs["score"], target=target)
            ctx.emit("review", f"场景组装 第 {rnd} 轮：{obs['score']:.1f}/10"
                     + (f"，客观相似度 DINO {objm['dino']:.3f} / SigLIP {objm['siglip']:.3f}" if objm else "")
                     + f"{'（冠军）' if won else '（未胜过冠军，回滚布局）'}"
                     + (f"；下一步 {decision['next_step']}" if decision else "；通过"), {"diffs": [d["observation"] for d in obs["diffs"]]})
            ctx.save()
            if passed:
                break
            champ = rec["rounds"][rec["champion"]]
            if not won:
                champ["decision"] = await jev.decide(subject=subject, observation=champ["observation"],
                                                     checks=champ["checks"], history=history,
                                                     model_key=route(ctx, "decide"), on_delta=ctx.delta_emitter())
            layout, last = champ["layout"], champ
        # 导出冠军布局
        champ = rec["rounds"][rec["champion"]]
        await hub.call("blender_compose_scene", {"items": [{**x, "glb": glbs[x["asset"]],
                                                            **_ground_flags(models.get(x["asset"], {})),
                                                            **({"ground": False} if _water_level(spec, x["asset"]) else {})}
                                                           for x in champ["layout"]]})
        fix_best = (rec.get("color_fix_best") or {}).get("fix")
        if fix_best:
            await hub.call("blender_recolor", {"fixes": fix_best, "roots": {x["id"]: x["asset"] for x in champ["layout"]}})
        exp = await hub.call("blender_export_glb", {"asset_name": "scene_composite"})
        if not exp.get("ok"):
            raise RuntimeError(f"场景导出失败：{exp.get('problems') or exp.get('error')}")
    rec["glb"] = exp["path"]
    (ctx.dir / "layout.json").write_text(json.dumps(champ["layout"], ensure_ascii=False, indent=1))
    ctx.artifact("glb", exp["path"], "场景组装.glb", asset="_scene", score=champ["score"],
                 triangles=exp.get("info", {}).get("triangles"), size=exp.get("info", {}).get("size"))
    ctx.artifact("layout", ctx.dir / "layout.json", "场景布局 layout.json")
    ctx.save()


async def stage_oskar_plan(ctx: RunContext) -> None:
    """Oskar 顾问 Agent：SAM 客观数据 → 读图问卷 → 场景程序（Bonsai 等由路由决定）。"""
    from .oskar import agent as OA
    refs = [a for a in ctx.store.artifacts(ctx.id) if a["kind"] == "reference"]
    if not refs:
        raise RuntimeError("Oskar 模式需要目标图（上传参考图或使用项目目标图）")
    target = refs[0]["path"]
    out = ctx.dir / "oskar"
    if ctx.options.get("oskar_program"):   # 直接给定程序（如人工参考程序），跳过读图
        pl = {"program": ctx.options["oskar_program"], "read": None, "facts": ""}
        out.mkdir(parents=True, exist_ok=True)
        (out / "program_plan.json").write_text(json.dumps(pl["program"], ensure_ascii=False, indent=1))
    else:
        pl = await OA.plan(target, out, model=route(ctx, "plan"), use_sam=bool(ctx.opt("oskar_sam", True)),
                           auto_palette=ctx.opt("oskar_palette", "") == "auto",
                           style_rules=style_agents.rules_text(ctx.options.get("style_profile"), "oskar") + consultation.revision_rules(ctx.options),
                           emit=lambda k, m, d=None: ctx.emit(k, m, d))
    ctx.state["oskar"] = {"target": target, **pl}
    ctx.state["scene_target"] = target
    ctx.artifact("spec", out / "program_plan.json", "场景程序（Oskar 引擎）")
    ctx.emit("result", f"场景程序：{len(pl['program'].get('items', []))} 个原语", {"program": pl["program"]})
    ctx.save()


async def _oskar_iterate(ctx: RunContext, st: dict, out: Path, asset: str, label: str, rounds: int) -> tuple[dict, dict]:
    """共用：Oskar 引擎构建迭代（硬检查 + 客观相似度 + Bonsai 受限编辑）并导出最佳版本 GLB。
    st 为 ctx.state 里的记录（含 target / program），就地写入 history、best_round、walk / paths 等。返回 (best, exp)。"""
    from .oskar import agent as OA
    st["history"] = []

    def slim(h: dict) -> dict:
        return {"round": h["round"], "metrics": h.get("metrics"), "accepted": h.get("accepted"),
                "checks": h.get("checks"), "notes": h.get("notes"), "edit_log": h.get("edit_log"),
                "diffs": [d.get("what") for d in (h.get("review") or {}).get("diffs", []) if isinstance(d, dict)],
                "images": h.get("images"), "error": h.get("error")}

    def on_round(h: dict) -> None:
        st["history"].append(slim(h))
        ctx.save()
        for p in h.get("images") or []:
            ctx.artifact("render", p, f"{label} 第{h['round']}轮", asset=asset, round=h["round"],
                         score=round(h.get("objective") or 0, 1), target=st["target"])
        m = h.get("metrics") or {}
        fails = [c["name"] for c in (h.get("checks") or []) if c.get("required") and not c["passed"]]
        ctx.emit("review", f"{label} 第 {h['round']} 轮：客观相似度 DINO {m.get('dino', 0):.3f} / SigLIP {m.get('siglip', 0):.3f}；"
                           f"硬检查{'全部通过' if not fails else '未过：' + '、'.join(fails)}；"
                           + ("初稿" if h["round"] == 0 else ("接受" if h.get("accepted") else "未提升，回滚")),
                 {k: h.get(k) for k in ("round", "metrics", "checks", "edit_log", "review", "accepted")})

    async with MCPHub(ctx.dir, servers=["blender"]) as hub:
        CAPS.write_run_plan(ctx, ctx.state.get("spec") or {})   # 体素层能力（REACode）：build_oskar 读取
        res = await OA.run(st["target"], hub, out, model=route(ctx, "patch"), rounds=rounds,
                           style_rules=style_agents.rules_text(ctx.options.get("style_profile"), "oskar") + consultation.revision_rules(ctx.options),
                           program=st["program"], emit=lambda k, m, d=None: ctx.emit(k, m, d), on_round=on_round,
                           patience=int(ctx.opt("oskar_patience", 3)))
        best = res["best"]
        built = await hub.call("blender_build_oskar", {"program": best["program"]})
        exp = await hub.call("blender_export_glb", {"asset_name": asset})
    if not exp.get("ok"):
        raise RuntimeError(f"导出失败：{exp.get('error') or exp.get('problems')}")
    (out / "program_best.json").write_text(json.dumps(best["program"], ensure_ascii=False, indent=1))
    st.update({"best_round": best["round"], "checks": best.get("checks"), "metrics": best.get("metrics") or {},
               "export_size": exp.get("info", {}).get("size"), "glb": exp["path"],
               "walk": built.get("walk") or [], "paths": built.get("paths") or [],
               "history": [slim(h) for h in res["history"]],
               "rounds": [{"round": h["round"], "score": h.get("objective"), "accepted": h.get("accepted")}
                          for h in res["history"]]})
    ctx.artifact("layout", out / "program_best.json", f"最佳场景程序（{label}）")
    if any(c.get("required") and not c["passed"] for c in best.get("checks") or []):
        ctx.emit("warn", f"{label} 最佳版本仍有硬检查未通过")
    ctx.save()
    return best, exp


async def stage_oskar_build(ctx: RunContext) -> None:
    """引擎构建 + 硬检查 + 渲染 + 客观相似度 + 评审受限编辑（客观分升才接受），最后导出最佳版本。"""
    st = ctx.state["oskar"]
    out = ctx.dir / "oskar"
    best, exp = await _oskar_iterate(ctx, st, out, "oskar_scene", "Oskar", int(ctx.opt("oskar_rounds", 6)))
    title = best["program"].get("scene") or ctx.run["title"]
    m = best.get("metrics") or {}
    ctx.state["spec"] = {"title": title, "assets": [{"id": "oskar_scene", "name": title, "kind": "environment",
                                                     "role": "scene"}]}
    ctx.state["concepts"] = {"oskar_scene": {"selected": 0, "candidates": [{"path": st["target"]}]}}
    ctx.state["models"] = {"oskar_scene": {"glb": exp["path"], "final_score": round(m.get("dino", 0.0), 3),  # 客观结构相似度 DINO
                                           "rounds": st["rounds"]}}
    ctx.artifact("glb", exp["path"], f"{title}.glb", asset="oskar_scene", score=m.get("dino"),
                 triangles=exp.get("info", {}).get("triangles"))
    ctx.save()


async def stage_world(ctx: RunContext) -> None:
    """完整游戏：世界（地形 / 岩体 / 建筑 / 桥 / 树 / 水）由 Oskar 引擎按关键画面生成，风格统一；
    道具与角色仍在 model 阶段单独建模，Godot 组装时放进这个世界。"""
    from .oskar import agent as OA
    target = _target_image(ctx, "_key_art")
    out = ctx.dir / "world"
    if ctx.options.get("world_program"):
        pl = {"program": ctx.options["world_program"], "read": None, "facts": ""}
    else:
        pl = await OA.plan(target, out, model=route(ctx, "plan"), use_sam=bool(ctx.opt("oskar_sam", True)),
                           auto_palette=ctx.opt("oskar_palette", "auto") == "auto",
                           style_rules=style_agents.rules_text(ctx.options.get("style_profile"), "oskar") + consultation.revision_rules(ctx.options),
                           emit=lambda k, m, d=None: ctx.emit(k, m, d))
    st = ctx.state["world"] = {"target": target, **pl}
    ctx.save()
    best, exp = await _oskar_iterate(ctx, st, out, "oskar_world", "Oskar 世界", int(ctx.opt("world_rounds", 4)))
    m = best.get("metrics") or {}
    ctx.artifact("glb", exp["path"], "Oskar 世界.glb", asset="oskar_world", score=m.get("dino"),
                 triangles=exp.get("info", {}).get("triangles"))
    ctx.emit("result", f"Oskar 世界：{len(best['program'].get('items', []))} 个原语，可站立点 {len(st['walk'])} 个，"
                       f"通路 {len(st['paths'])} 条；与关键画面 DINO {m.get('dino', 0):.3f}")
    ctx.save()


async def stage_oskar_godot(ctx: RunContext) -> None:
    """把最佳版本的 GLB 接入 Godot：确定性生成展示工程（同机位同光照、轨道相机、碰撞体），试运行截图并与 Blender 渲染核对。"""
    from .godot import oskar_scene as OG
    st = ctx.state["oskar"]
    best = next((h for h in st.get("history", []) if h["round"] == st.get("best_round")), None) or {}
    imgs = best.get("images") or []
    views = {("hero_left" if "hero_left" in p else "hero"): p for p in imgs}
    async with MCPHub(ctx.dir, godot_project=_godot_project(ctx), servers=["godot"]) as hub:
        res = await OG.setup_and_verify(hub, _godot_project(ctx), blender_views=views or None,
                                        blender_size=st.get("export_size"), target=st.get("target"),
                                        walk=st.get("walk"), paths=st.get("paths"),
                                        emit=lambda k, m, d=None: ctx.emit(k, m, d))
    for p, label in zip(res["shots"], res["labels"]):
        ctx.artifact("screenshot", p, f"Godot · {label}", asset="oskar_scene", shot=label)
    st["godot"] = {k: res[k] for k in ("ok", "checks", "shots", "labels", "match", "to_target", "fps", "project")}
    ctx.save()
    fails = [c for c in res["checks"] if c["required"] and not c["passed"]]
    ctx.emit("review", "Godot 接入：" + ("全部核对通过" if not fails else "未通过：" + "；".join(
        f"{c['name']}（{c['detail']}）" for c in fails)) + (f"；同机位相似度 {res['match']}" if res["match"] else ""),
        {"checks": res["checks"], "match": res["match"], "to_target": res["to_target"]})
    if fails:
        raise RuntimeError("Godot 接入核对未通过：" + "；".join(f"{c['name']} {c['detail']}" for c in fails))


STAGE_FUNCS: dict[str, Callable[[RunContext], Awaitable[None]]] = {
    "assembly_plan": assembly.plan, "journey": assembly.journey,
    "planet_compose": planet_assembly.compose, "planet_godot": planet_assembly.godot,
    "plan": stage_plan, "concept": stage_concept, "model": stage_model, "assemble": stage_assemble,
    "review": stage_review, "deliver": stage_deliver, "decompose": stage_decompose, "compose": stage_compose,
    "oskar_plan": stage_oskar_plan, "oskar_build": stage_oskar_build, "oskar_godot": stage_oskar_godot,
    "world": stage_world, "learn": style_agents.learn, "consult": consultation.consult,
    "team_plaza": teams.plaza, "team_retro": teams.retro, "team_research": team_research.research,
    "atlas_brief": world_atlas.brief, "atlas_targets": world_atlas.targets, "atlas_scenes": world_atlas.scenes,
    "atlas_compose": world_atlas.compose, "atlas_godot": world_atlas.godot,
}


# ───────────────────────────── 运行队列 ─────────────────────────────

class Orchestrator:
    def __init__(self, store: Store, *, start_worker: bool = True) -> None:
        self.store = store
        self.controls: dict[str, RunControl] = {}
        self.start_from: dict[str, str] = {}
        self._wake = threading.Event()
        self.current: str | None = None
        self._revision_lock = threading.RLock()
        self._queue_lock = threading.RLock()
        if not start_worker:
            return
        for r in store.list_runs(500):
            if r["status"] in ("running", "waiting", "paused"):
                store.update_run(r["id"], status="interrupted", error="控制台重启时运行被中断，可从当前阶段重试")
        threading.Thread(target=self._loop, daemon=True, name="pipeline-runner").start()

    def control(self, run_id: str) -> RunControl:
        return self.controls.setdefault(run_id, RunControl())

    def submit(self, title: str, brief: str, mode: str, options: dict, start: bool = True) -> str:
        if mode == "assembly":
            raise ValueError("请通过游戏组装页面选择已验收成果")
        agent_id = options.get("style_agent", "oskar")
        options = {**options, "style_agent": agent_id, "style_profile": style_agents.snapshot(agent_id)}
        if mode == "learn":
            options['learning_sources'] = style_agents.learning_sources(agent_id, options.get('learning_source_ids', []))
        if mode == 'consult' and not options.get('consultation'):
            raise ValueError('请从已有作品邀请顾问')
        stages = STAGES[mode]
        from . import agent_config
        acfg = options["style_profile"].get("config") or {}
        if acfg.get("image_backend") and not options.get("image_backend"):
            options["image_backend"] = acfg["image_backend"]
        if teams.is_team(options["style_profile"]) and mode == "learn":
            stages = ["team_research"]                     # 团队学习 = 成员分头调研（证据分级）→ 组长汇总
        if teams.is_team(options["style_profile"]) and mode not in ("learn", "consult", "asset"):
            mode, stages = "game", STAGES["team_game"]     # 团队任务仍是“完整游戏”，只是多了议事与复盘
        if "approval_gates" not in options or not options["approval_gates"]:   # 没手动指定：按 Agent 自主级别
            options["approval_gates"] = [g for g in agent_config.gates_for(options["style_profile"])
                                         if g in stages or g == "concept" and "concept" in stages]
        run_id = self.store.create_run(title or brief[:30], brief, mode, options, stages,
                                       status='queued' if start else 'preparing')
        if start:
            self._wake.set()
        return run_id

    def compose_game(self, title, brief, selections, agent_id='oskar', request_id='', form='levels') -> str:
        import uuid
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('请提供有效的组装请求编号')
        with self._revision_lock:
            existing = next((r for r in self.store.list_runs(10000)
                             if r['options'].get('assembly_request_id') == request_id), None)
            if existing:
                return existing['id']
            manifest = assembly.prepare(self.store, title, brief, selections, form or 'levels')
            options = {'style_agent': agent_id, 'style_profile': style_agents.snapshot(agent_id),
                       'assembly_request_id': request_id, 'approval_gates': ['journey']}
            run_id = self.store.create_run(title.strip(), brief.strip(), 'assembly', options,
                                           STAGES['assembly_planet' if manifest['form'] == 'planet' else 'assembly'],
                                           status='preparing')
            directory = assembly.root()/run_id
            try:
                assembly.freeze(manifest, directory)
                options['assembly'] = manifest
                self.store.update_run(run_id, options=options)
                self.store.add_artifact(run_id, 'assembly_plan', 'assembly_manifest',
                                        directory/'assembly-manifest.json', '游戏组装来源与顺序')
                self.enqueue(run_id)
            except Exception as exc:
                self.store.update_run(run_id, status='failed', error=f'组装输入准备失败：{exc}')
                raise
            return run_id

    def invite(self, parent_id: str, agent_id: str, brief: str, areas: list[str], preserve: str = '') -> str:
        if not isinstance(brief,str) or not brief.strip() or len(brief)>4000:
            raise ValueError('请填写审阅要求（最多 4000 字）')
        evidence = consultation.capture(self.store, parent_id, areas, preserve)
        run_id = self.submit(f"{style_agents.get(agent_id)['name']} · 审阅 {evidence['title']}", brief.strip(),
                             'consult', {'style_agent':agent_id, 'consultation':evidence,
                                         'parent_run':parent_id}, start=False)
        try:
            directory = config.resolve(config.get('pipeline.runs_dir','runs'))/run_id
            directory.mkdir(parents=True,exist_ok=True)
            consultation.freeze_inputs(evidence, directory)
            options = self.store.get_run(run_id)['options']
            options['consultation'] = evidence
            self.store.update_run(run_id,options=options)
            style_agents._save(directory/'consultation-inputs.json',evidence)
            self.enqueue(run_id)
        except Exception as exc:
            self.store.update_run(run_id,status='failed',error=f'准备审阅快照失败：{exc}')
            raise
        return run_id

    def revise(self, consult_id: str, version: str, selected: list[str]) -> str:
        with self._revision_lock:
            run = self.store.get_run(consult_id)
            if not run or run['mode'] != 'consult':
                raise ValueError('请选择一个已完成的顾问提案')
            root = config.resolve(config.get('pipeline.runs_dir','runs'))
            path = root/consult_id/'state.json'
            request,data = consultation.revision_request(run, json.loads(path.read_text()) if path.is_file() else {}, version, selected)
            key = consultation.digest(request)
            existing = next((r for r in self.store.list_runs(10000) if r['options'].get('revision_key') == key),None)
            if existing:
                return existing['id']
            profile = run['options']['style_profile']
            brief = request['original_brief']+'\n\n根据已审核的顾问提案修改已有模型。\n必须保留：'+(request['preserve'] or '原作身份')
            for change in request['changes']:
                brief += f"\n\n{change['title']}：{change['change']}\n验收办法：{change['acceptance']}"
            options = {**run['options']['consultation']['revision_support'].get('options',{}),
                       'style_agent':profile['agent_id'],'style_profile':profile,'revision':request,
                       'revision_key':key,'parent_run':request['parent_id'],'model':run['options'].get('model','main'),
                       'approval_gates':['concept'],'model_mode':'agent'}
            run_id = self.store.create_run(f"{request['title']} · {profile['name']} 改版",brief,'asset',options,
                                           STAGES['asset'],status='preparing')
            try:
                directory = root/run_id
                source = directory/'revision-inputs'/'original.glb'
                source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(data)
                consultation.freeze_revision_baseline(request, directory)
                options['source_model'] = str(source)
                self.store.update_run(run_id,options=options)
                style_agents._save(directory/'revision-request.json',request)
                self.store.add_artifact(run_id,'plan','revision_request',directory/'revision-request.json','审核后的改版要求')
                self.enqueue(run_id)
            except Exception as exc:
                self.store.update_run(run_id,status='failed',error=f'准备改版失败：{exc}')
                raise
            return run_id

    def enqueue(self, run_id: str) -> None:
        self.store.update_run(run_id, status="queued")
        self._wake.set()

    def retry(self, run_id: str, from_stage: str | None = None, options: dict | None = None) -> None:
        with self._queue_lock:
            self._retry(run_id, from_stage, options)

    def _retry(self, run_id: str, from_stage: str | None = None, options: dict | None = None) -> None:
        if self.current == run_id:  # 正在执行的运行不能重试（否则状态被改回“排队”，结束后会被再跑一遍）
            raise RuntimeError("该运行正在执行，不能重试；如需重跑请先取消")
        run = self.store.get_run(run_id)
        if run is None:
            raise RuntimeError("任务不存在")
        stages = self.store.stages(run_id)
        if not stages:
            raise RuntimeError("该任务没有可重试的阶段")
        if from_stage is None:
            from_stage = next((s["name"] for s in stages if s["status"] not in ("done", "skipped")), stages[0]["name"])
        names = [s["name"] for s in stages]
        if from_stage not in names:
            raise RuntimeError(f"该任务没有阶段：{from_stage}")
        if from_stage == "journey":
            # 人工试玩确认要求前面的自动验收已通过；没通过时从有问题的阶段重跑，否则重跑会立刻再失败
            try:
                state_path = config.resolve(config.get("pipeline.runs_dir", "runs")) / run_id / "state.json"
                st = json.loads(state_path.read_text()) if state_path.exists() else {}
                issues = [c for c in assess(run, st)["issues"] if c["stage"] != "journey"]
            except (OSError, ValueError, KeyError, TypeError):
                issues = []
            earlier = [c["stage"] for c in issues if c["stage"] in names and names.index(c["stage"]) < names.index("journey")]
            if earlier:
                from_stage = min(earlier, key=names.index)
                self.store.event(run_id, None, "info", f"自动验收未通过（{'、'.join(c['name'] for c in issues[:3])}），"
                                                      f"改为从「{STAGE_LABELS.get(from_stage, from_stage)}」重跑")
        assembly.validate(run["options"], config.resolve(config.get("pipeline.runs_dir", "runs"))/run_id)
        consultation.validate_revision_inputs({**run['options'], **(options or {})},
            config.resolve(config.get('pipeline.runs_dir','runs'))/run_id)
        if options:
            self.store.update_run(run_id, options={**run["options"], **options})
        for s in names[names.index(from_stage):]:
            self.store.set_stage(run_id, s, "pending")
        self.start_from[run_id] = from_stage
        c = self.control(run_id)
        c.cancelled = False
        c.paused = False
        c.approvals.clear()
        c.requests.clear()
        self.store.update_run(run_id, status="queued", error=None)
        _ = run
        self._wake.set()

    def control_action(self, run_id: str, action: str) -> None:
        """Serialize queue ownership and UI controls; active work stops at safe points."""
        with self._queue_lock:
            c = self.control(run_id)
            with c.lock:
                run = self.store.get_run(run_id)
                if not run:
                    raise RuntimeError("任务不存在")
                status = run['status']
                if action == 'pause':
                    if status == 'paused':
                        return
                    if status not in ('queued', 'running', 'waiting'):
                        raise RuntimeError('只有排队或执行中的任务可以暂停')
                    c.paused = True
                    self.store.update_run(run_id, status='paused')
                elif action == 'resume':
                    if status != 'paused':
                        raise RuntimeError('该任务没有暂停')
                    c.paused = False
                    # A held queue entry must return to the queue, never become an orphan runner.
                    waiting = any(s['status'] == 'waiting' for s in self.store.stages(run_id))
                    target = ('waiting' if waiting else 'running') if self.current == run_id else 'queued'
                    self.store.update_run(run_id, status=target)
                    self._wake.set()
                elif action == 'cancel':
                    if status == 'cancelled':
                        return
                    if status not in ('queued', 'paused', 'running', 'waiting', 'interrupted', 'failed'):
                        raise RuntimeError('该任务已经结束或正在准备，无法取消')
                    c.cancelled = True
                    c.paused = False
                    if self.current != run_id:
                        self.store.update_run(run_id, status='cancelled')
                else:
                    raise RuntimeError('未知任务控制操作')

    def _claim_next(self) -> dict | None:
        with self._queue_lock:
            if self.current is not None:
                return None
            run = self._next()
            if run:
                self.current = run['id']
                self.store.update_run(run['id'], status='running')
            return run

    def _next(self) -> dict | None:
        queued = [r for r in self.store.list_runs(500) if r["status"] == "queued"]
        return sorted(queued, key=lambda r: r["created"])[0] if queued else None

    def _loop(self) -> None:
        while True:
            run = self._claim_next()
            if not run:
                self._wake.wait(3)
                self._wake.clear()
                continue
            try:
                asyncio.run(self._execute(run))
            except Exception:  # 保底：不让运行线程退出
                traceback.print_exc()
            finally:
                with self._queue_lock:
                    self.current = None

    async def _execute(self, run: dict) -> None:
        control = self.control(run["id"])
        control.model_key = run["options"].get("model", "main")
        try:
            ctx = RunContext(self.store, run, control)
            if not isinstance(ctx.state, dict):
                raise ValueError('state.json 必须是任务状态对象，请恢复有效快照')
            consultation.validate_revision_inputs(ctx.options, ctx.dir)
            if run["mode"] == "assembly" and not ctx.options.get("assembly"):
                raise ValueError("组装输入准备不完整，请重新选择成果创建任务")
            assembly.validate(ctx.options, ctx.dir)
        except Exception as exc:
            # Initialization happens before stage error handling; never leave a phantom runner.
            with control.lock:
                control.paused = False
                self.store.update_run(run['id'], status='failed', error=f'任务输入检查失败：{exc}')
            self.store.event(run['id'], '', 'error', f'任务输入检查失败：{exc}')
            return
        with control.lock:
            self.store.update_run(run["id"], status="paused" if control.paused else "running", error=None)
        start = self.start_from.pop(run["id"], None)
        stages = self.store.stages(run["id"])
        begun = start is None
        for s in stages:
            name = s["name"]
            if not begun:
                if name != start:
                    continue
                begun = True
            if s["status"] in ("done", "skipped") and start is None:
                continue
            ctx.stage = name
            self.store.update_run(run["id"], current_stage=name)
            self.store.set_stage(run["id"], name, "running")
            ctx.emit("stage", f"开始阶段：{STAGE_LABELS.get(name, name)}")
            t0 = time.time()
            try:
                with compute.context(ctx.id, name, ctx.emit), teams.attribution(ctx, name):
                    await control.checkpoint()
                    await STAGE_FUNCS[name](ctx)
                    await control.checkpoint()
            except Cancelled:
                self.store.set_stage(run["id"], name, "cancelled")
                with control.lock:
                    control.paused = False
                    self.store.update_run(run["id"], status="cancelled")
                ctx.emit("cancelled", "运行已取消")
                return
            except Exception as e:
                tb = traceback.format_exc()
                self.store.set_stage(run["id"], name, "failed", {"error": str(e)})
                with control.lock:
                    control.paused = False
                    self.store.update_run(run["id"], status="failed", error=f"{name}: {e}")
                ctx.emit("error", f"阶段失败：{e}", {"traceback": tb[-4000:]})
                return
            self.store.set_stage(run["id"], name, "done", {"seconds": round(time.time() - t0)})
            ctx.emit("stage_done", f"完成阶段：{STAGE_LABELS.get(name, name)}（{time.time() - t0:.0f}s）")
        with control.lock:
            control.paused = False
            if control.cancelled:
                self.store.update_run(run["id"], status="cancelled")
                return
            self.store.update_run(run["id"], status="done", current_stage=None)
        if run["mode"] not in ("learn", "consult") and "team_retro" not in [s["name"] for s in stages]:
            try:   # OPC：一人完成全部阶段，同样结算 token 与效能、推进棘轮（团队在复盘阶段结算）
                res = teams.settle(ctx)
                ctx.emit("result", f"结算：token {res['tokens']} · 效能 {res['efficiency']:.4f}（{res['ratchet']['reason']}）", res)
            except Exception as exc:  # 结算失败不影响成果
                ctx.emit("info", f"效能结算未完成：{exc}")
        if run["mode"] not in ("learn", "consult") and ctx.options.get("learn_after_run"):
            learning_id = self.submit(f"{ctx.options.get('style_profile', {}).get('name', 'Agent')} · 创作复盘",
                                     "从这次创作的质量检查、有效修改与失败尝试中提炼可复用的风格和构建规则。",
                                     "learn", {"style_agent": ctx.options.get("style_agent", "oskar"), "learning_runs": [ctx.id]})
            ctx.emit("result", "已安排创作复盘，学习草稿需在系统 Agent 页面审核应用。", {"learning_run": learning_id})
