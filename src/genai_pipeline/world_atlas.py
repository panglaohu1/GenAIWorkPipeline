"""atlas 模式：Oskar 导演的多场景世界。

atlas_brief → atlas_targets → atlas_scenes → atlas_compose → atlas_godot → deliver
  世界圣经      目标图（逐张打分）  逐场景趋近        旅程弧线整合     Godot 核对

每一步的决策者都是任务快照里的风格 Agent（默认 Oskar）：评审维度、阶段、阈值、风格锁与整合原则
来自 Agent 定义的 direction，任务开始时固定。场景状态写在 state["atlas"]，可从任意阶段重跑；
已完成的场景 / 已确认的目标图在重跑时保留（删除对应记录即可重做）。
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from . import config, consultation, style_agents, vision
from .mcp_hub import MCPHub
from .oskar import director as D
from .oskar import planet as PL
from . import planet_assembly


def _profile(ctx) -> dict:
    return ctx.options.get("style_profile") or {}


def _direction(ctx) -> dict:
    atlas = ctx.state.get("atlas") or {}
    return atlas.get("direction") or D.resolve_direction(_profile(ctx).get("direction"))


def _rules(ctx, stage: str) -> str:
    return style_agents.rules_text(ctx.options.get("style_profile"), stage) + consultation.revision_rules(ctx.options)


def _say(ctx):
    return lambda kind, msg, data=None: ctx.emit(kind, msg, data)


def _agent_name(ctx) -> str:
    return _profile(ctx).get("name") or "Oskar"


def tool(ctx, name: str, what: str, args: dict | None = None) -> None:
    """Oskar 亲自调用工具：每次调用都以「Oskar → 工具」记在日志里，便于回看它是怎样工作的。"""
    ctx.emit("tool_call", f"{_agent_name(ctx)} → {name}：{what}", {"args": args or {}})


def _journal(ctx) -> str:
    atlas = ctx.state.get("atlas") or {}
    return "\n".join(x for x in (D.journal_text(atlas.get("prior_journal") or [], "过往世界的工作室手记"),
                                  D.journal_text(atlas.get("journal") or [], "本世界前几站的工作室手记")) if x)


def _scene(bible: dict, sid: str) -> dict:
    return next(s for s in bible["scenes"] if s["id"] == sid)


def _dims_brief(scores: dict | None, dims: list[dict]) -> str:
    if not scores:
        return "无维度分"
    return " ".join(f"{d['name']} {scores.get(d['id'], 0):.0f}" for d in dims)


# ───────────────────────────── 1. 世界圣经 ─────────────────────────────

async def brief(ctx) -> None:
    from .pipeline import route
    direction = D.resolve_direction(_profile(ctx).get("direction"))
    n = int(ctx.opt("atlas_scenes", 3))
    refs = [a["path"] for a in ctx.store.artifacts(ctx.id) if a["kind"] == "reference"]
    given = ctx.options.get("atlas_bible")
    if given:
        err = D.validate_bible(given)
        if err:
            raise ValueError(f"给定的世界圣经无效：{err}")
        bible = D.normalize_bible(given)
    else:
        ctx.emit("info", f"{_agent_name(ctx)} 正在构想世界：先定支柱、色彩角色与旅程节奏，再定每一站…")
        tool(ctx, "LLM.world_bible", f"构想 {n} 站的世界", {"scenes": n})
        bible = await D.world_bible(ctx.run["brief"], model=route(ctx, "plan"), direction=direction,
                                    style_rules=_rules(ctx, "plan"), n=n, refs=refs or None,
                                    on_delta=ctx.delta_emitter())
    prev = ctx.state.get("atlas") or {}
    agent_id = ctx.options.get("style_agent", "oskar")
    try:
        prior = style_agents.recent_journals(agent_id, exclude=ctx.id)
    except (ValueError, OSError):
        prior = []
    ctx.state["atlas"] = {"bible": bible, "direction": direction, "order": [s["id"] for s in bible["scenes"]],
                          "anchor": D.anchor_id(bible), "scenes": {}, "agent": _agent_name(ctx),
                          "inherited_from": direction.get("inherited_from"), "prior_journal": prior, "journal": []}
    if prior:
        ctx.emit("info", f"{_agent_name(ctx)} 翻开工作室手记：带上过往 {len({e['run_id'] for e in prior})} 个世界的 {len(prior)} 条经验")
    if prev.get("bible") and prev["bible"] != bible:   # 世界变了：旧目标图与场景不再适用
        ctx.state.pop("concepts", None)
        ctx.state.pop("models", None)
    ctx.state["spec"] = {"title": bible["title"], "pitch": bible.get("vision", ""), "art_style": bible.get("art_style", ""),
                         "assets": [{"id": s["id"], "name": s["name"], "kind": "environment", "role": "scene",
                                     "description": f"{D.ROLES[s['role']]} · {s.get('dominant', '')}"}
                                    for s in bible["scenes"]]}
    out = ctx.dir / "atlas"
    out.mkdir(parents=True, exist_ok=True)
    (out / "bible.json").write_text(json.dumps(bible, ensure_ascii=False, indent=2))
    ctx.artifact("spec", out / "bible.json", f"世界圣经 · {bible['title']}")
    ctx.store.update_run(ctx.id, title=bible["title"])
    ctx.save()
    journey = " → ".join(f"{s['name']}（{D.ROLES[s['role']]} {s['intensity']}）" for s in bible["scenes"])
    ctx.emit("result", f"{_agent_name(ctx)}：「{bible.get('vision', '')}」 旅程：{journey}", {"bible": bible})


# ───────────────────────────── 2. 目标图 ─────────────────────────────

def _selected_path(entry: dict | None) -> str | None:
    if not entry or not entry.get("candidates"):
        return None
    return entry["candidates"][entry.get("selected", 0) or 0]["path"]


async def targets(ctx) -> None:
    from .pipeline import route
    atlas = ctx.state["atlas"]
    atlas["bible"] = D.coerce_text(atlas["bible"])   # 旧圣经里被写成数组的文本字段（重跑时修正）
    bible, direction = atlas["bible"], _direction(ctx)
    conv = direction["convergence"]
    dims = D.rubric(direction)
    n = max(1, int(ctx.opt("concept_candidates", 3)))
    rounds = max(1, int(ctx.opt("atlas_target_rounds", conv.get("target_rounds", 2))))
    pass_score = float(conv.get("target_pass", 6.5))
    backend = ctx.options.get("image_backend") or config.get("image.default_backend")
    concepts: dict[str, dict] = ctx.state.setdefault("concepts", {})
    order_index = {s["id"]: i for i, s in enumerate(bible["scenes"])}
    name = _agent_name(ctx)

    async def generate(sid: str, content: str, count: int) -> None:
        scene = _scene(bible, sid)
        entry = concepts[sid]
        out_dir = ctx.dir / "concept" / sid
        out_dir.mkdir(parents=True, exist_ok=True)
        full = D.target_prompt(bible, scene, direction, content)
        for k in range(count):
            await ctx.control.checkpoint()
            idx = len(entry["candidates"])
            out = out_dir / f"cand_{idx:02d}.png"
            tool(ctx, f"{backend.upper()}.draw", f"画「{scene['name']}」目标图 #{idx}（世界风格锁）", {"prompt": full})
            res = await vision.generate_image(full, out, backend=backend, seed=2000 + order_index[sid] * 101 + idx * 7919)
            aid = ctx.artifact("concept", out, f"{scene['name']} #{idx}", asset=sid, index=idx, prompt=full,
                               seconds=res.get("seconds"), backend=res.get("backend"))
            entry["candidates"].append({"path": str(out), "artifact": aid, "prompt": full})
            ctx.save()

    async def judge(sid: str) -> dict:
        scene = _scene(bible, sid)
        entry = concepts[sid]
        window = entry["candidates"][-6:]   # 每次评审最多 6 张（最新的），旧的分数保留
        offset = len(entry["candidates"]) - len(window)
        anchor = None if sid == atlas["anchor"] else _selected_path(concepts.get(atlas["anchor"]))
        tool(ctx, "VLM.judge", f"按评审维度审看「{scene['name']}」的 {len(window)} 张目标图"
                               + ("，对照世界母题" if anchor else ""))
        j = await D.judge_targets([c["path"] for c in window], bible=bible, scene=scene, direction=direction,
                                  model=route(ctx, "observe"), anchor=anchor, style_rules=_rules(ctx, "review"),
                                  on_delta=ctx.delta_emitter())
        for i, c in enumerate(j["candidates"]):
            entry["candidates"][offset + i]["oskar"] = c
            ctx.store.update_artifact_meta(entry["candidates"][offset + i]["artifact"], score=c["director"],
                                           oskar=c["scores"], buildable=c["buildable"])
        best = offset + j["best"]
        entry["selected"] = best
        for i, c in enumerate(entry["candidates"]):
            ctx.store.update_artifact_meta(c["artifact"], selected=(i == best))
        rec = {"best": best, "director": j["candidates"][j["best"]]["director"],
               "buildable": j["candidates"][j["best"]]["buildable"], "direction": j["direction"],
               "revised_prompt": j["revised_prompt"], "consistency": j["consistency"], "anchor": bool(anchor),
               "action": j["action"]}
        entry.setdefault("oskar", {}).setdefault("attempts", []).append(rec)
        ctx.save()
        c = j["candidates"][j["best"]]
        ctx.emit("review", f"{name} · {scene['name']} 目标图：选 #{best}，导演分 {c['director']:.1f}"
                           f"（{_dims_brief(c['scores'], dims)}）{'' if c['buildable'] else '，引擎难以复刻'}"
                           + (f"，与世界母题风格一致性 {c['consistency']:.3f}" if c.get("consistency") is not None else "")
                           + f" — {j['direction']}", {"scene": sid, **rec, "scores": c["scores"]})
        return rec

    refs = [a for a in ctx.store.artifacts(ctx.id) if a["kind"] == "reference"]
    for scene in D.target_order(bible):
        sid = scene["id"]
        entry = concepts.setdefault(sid, {"candidates": [], "prompt": scene["key_art_prompt"]})
        if (entry.get("oskar") or {}).get("done"):
            continue
        ref = refs[order_index[sid]] if ctx.options.get("reference_as_target") and order_index[sid] < len(refs) else None
        if ref and not entry["candidates"]:
            entry["candidates"].append({"path": ref["path"], "artifact": ref["id"], "prompt": "(用户参考图)"})
            ctx.emit("info", f"{scene['name']}：使用用户参考图作为目标图，{name} 仍按评审维度打分")
            await judge(sid)
        else:
            content = entry.get("content") or scene["key_art_prompt"]
            for attempt in range(rounds):
                await generate(sid, content, n)
                rec = await judge(sid)
                if rec["director"] >= pass_score and rec["buildable"] and rec["action"] != "redraw":
                    break
                if attempt + 1 < rounds and rec["revised_prompt"]:
                    content = rec["revised_prompt"]
                    entry["content"] = content
                    ctx.emit("info", f"{name}：{scene['name']} 最好的一张只有 {rec['director']:.1f}（目标 {pass_score:g}），"
                                     f"按美术指导改写提示词重画 — {rec['direction']}")
        entry["oskar"]["done"] = True
        entry["prompt"] = entry.get("content") or scene["key_art_prompt"]
        ctx.save()

    async def handle(req: dict) -> None:
        if req.get("type") == "regenerate" and req.get("asset") in concepts:
            sid = req["asset"]
            content = req.get("prompt") or concepts[sid].get("content") or _scene(bible, sid)["key_art_prompt"]
            concepts[sid]["content"] = content
            await generate(sid, content, int(req.get("count", 2)))
            await judge(sid)

    payload = await ctx.wait_approval("concept", handle)
    for sid, art_id in (payload.get("selections") or {}).items():
        entry = concepts.get(sid)
        if not entry:
            continue
        for i, c in enumerate(entry["candidates"]):
            if c["artifact"] == art_id:
                entry["selected"] = i
            ctx.store.update_artifact_meta(c["artifact"], selected=(c["artifact"] == art_id))
    summary = {}
    for s in bible["scenes"]:
        entry = concepts[s["id"]]
        picked = entry["candidates"][entry["selected"]]
        oskar_pick = (entry.get("oskar") or {}).get("attempts", [{}])[-1].get("best")
        summary[s["id"]] = {"path": picked["path"], "director": (picked.get("oskar") or {}).get("director"),
                            "buildable": (picked.get("oskar") or {}).get("buildable", True),
                            "human_override": oskar_pick is not None and oskar_pick != entry["selected"],
                            "human_confirmed": "concept" in (ctx.opt("approval_gates", []) or [])}
    atlas["targets"] = summary
    ctx.save()


# ───────────────────────────── 3. 逐场景趋近 ─────────────────────────────

def _slim(h: dict) -> dict:
    crit = h.get("critique") or {}
    return {"round": h["round"], "phase": h.get("phase"), "director": h.get("director"), "scores": crit.get("scores"),
            "verdict": crit.get("verdict"), "keep": crit.get("keep"), "metrics": h.get("metrics"),
            "objective": h.get("objective"), "checks": h.get("checks"), "checks_ok": h.get("checks_ok"),
            "accepted": h.get("accepted"), "decision": h.get("decision"), "edit_log": h.get("edit_log"),
            "edits": [e.get("what") for e in h.get("edits") or [] if isinstance(e, dict)],
            "images": h.get("images"), "notes": h.get("notes"), "error": h.get("error")}


async def scenes(ctx) -> None:
    from .pipeline import _target_image, route
    from .oskar import agent as OA
    atlas = ctx.state["atlas"]
    atlas["bible"] = D.coerce_text(atlas["bible"])
    bible, direction = atlas["bible"], _direction(ctx)
    conv = direction["convergence"]
    dims = D.rubric(direction)
    max_rounds = int(ctx.opt("atlas_rounds", conv.get("max_rounds", 4)))
    min_dino = float(config.get("metrics.pass_min_dino", 0.25))
    name = _agent_name(ctx)
    given = ctx.options.get("atlas_programs") or {}
    models = ctx.state.setdefault("models", {})
    async with MCPHub(ctx.dir, servers=["blender"]) as hub:
        for scene in D.target_order(bible):    # 高潮场景先建：它的配色成为全世界的色彩角色
            sid = scene["id"]
            rec = atlas["scenes"].setdefault(sid, {})
            if rec.get("status") == "done" and rec.get("glb") and Path(rec["glb"]).exists():
                continue
            target = _target_image(ctx, sid)
            out = ctx.dir / "atlas" / sid
            out.mkdir(parents=True, exist_ok=True)
            pal = atlas.get("palette")
            if given.get(sid):
                plan = {"program": given[sid], "read": None, "facts": ""}
            else:
                tool(ctx, "SAM3 + LLM.plan", f"读「{scene['name']}」目标图，写场景程序（只定大形）")
                plan = await OA.plan(target, out, model=route(ctx, "plan"), use_sam=bool(ctx.opt("oskar_sam", True)),
                                     auto_palette=pal is None and (bible.get("palette") or {}).get("base") == "auto",
                                     style_rules=_rules(ctx, "oskar") + D.scene_rules(bible, scene) + "\n" + _journal(ctx),
                                     emit=_say(ctx))
            program = plan["program"]
            if pal is None:   # 世界色彩角色：取自第一个建造的场景（高潮），之后所有场景共用
                value = program.get("palette") or (bible.get("palette") or {}).get("base") or "citadel"
                if value == "auto":
                    value = "citadel"
                pal = atlas["palette"] = {"value": value, "base": program.get("palette_base"), "from": sid}
                ctx.emit("result", f"{name}：世界色彩角色取自「{scene['name']}」"
                                   f"（{'目标图自动取色' if isinstance(value, dict) else value}），其余场景共用", pal)
            program = D.lock_program(program, bible, scene, pal["value"], pal.get("base"))
            (out / "program_plan.json").write_text(json.dumps(program, ensure_ascii=False, indent=1))
            rec.clear()
            rec.update(status="iterating", target=target, read=plan.get("read"), program_plan=program, history=[])
            ctx.save()

            def on_round(h: dict, scene=scene, rec=rec) -> None:
                rec["history"].append(_slim(h))
                ctx.save()
                for p in h.get("images") or []:
                    ctx.artifact("render", p, f"{scene['name']} 第{h['round']}轮", asset=scene["id"], round=h["round"],
                                 score=h.get("director"), objective=round(h.get("objective") or 0, 1), target=rec["target"])
                m = h.get("metrics") or {}
                fails = [c["name"] for c in (h.get("checks") or []) if c.get("required") and not c["passed"]]
                crit = h.get("critique") or {}
                ctx.emit("review",
                         f"{name} · {scene['name']} 第 {h['round']} 轮【{(h.get('phase') or {}).get('name', '')}】"
                         + (f"导演分 {h['director']:.1f}（{_dims_brief(crit.get('scores'), dims)}）" if h.get("director") is not None else "导演未评分")
                         + (f"；DINO {m.get('dino', 0):.3f} / SigLIP {m.get('siglip', 0):.3f}" if m else "；客观相似度不可用")
                         + f"；硬检查{'全部通过' if not fails else '未过：' + '、'.join(fails)}；{h.get('decision', '')}"
                         + (f"；下一步：{D.ACTIONS.get(crit.get('action'), '')}" if crit.get("action") else "")
                         + (f" — {crit['verdict']}" if crit.get("verdict") else ""),
                         {"scene": scene["id"], **_slim(h)})

            async def evaluate(pg, tag, sid=sid, target=target, scene=scene):
                tool(ctx, "Blender.build_oskar + render + metrics", f"建造并渲染「{scene['name']}」{tag}，与目标图比较")
                return await OA.evaluate_program(hub, target, pg, f"{sid}_{tag}")

            async def critique(r, phase, failed, scene=scene, target=target):
                tool(ctx, "VLM.critique", f"审看「{scene['name']}」第 {r['round']} 轮渲染【{phase.get('name', '')}】")
                return await D.critique(r, phase, failed, target=target, bible=bible, scene=scene, direction=direction,
                                        model=route(ctx, "observe"), style_rules=_rules(ctx, "review"),
                                        journal=_journal(ctx), can_replan=phase.get("can_replan", True))

            async def replan(best, failed, scene=scene, target=target, out=out):
                verdict = (best.get("critique") or {}).get("verdict") or ""
                tool(ctx, "LLM.plan", f"推翻「{scene['name']}」当前程序，重新读图写一版 — {verdict}")
                why = ("\n\n推倒重来：上一版的问题——" + verdict + "；已失败的改法——"
                       + "；".join(f.get("reason", "") for f in failed[-3:]) + "。这一版换一种大形组织方式。")
                again = await OA.plan(target, out / "replan", model=route(ctx, "plan"), use_sam=False,
                                      style_rules=_rules(ctx, "oskar") + D.scene_rules(bible, scene) + "\n" + _journal(ctx) + why,
                                      emit=_say(ctx))
                return D.lock_program(again["program"], bible, scene, atlas["palette"]["value"], atlas["palette"].get("base"))

            res = await D.converge(program, evaluate=evaluate, critique=critique, direction=direction,
                                   max_rounds=max_rounds, min_dino=min_dino, on_round=on_round,
                                   checkpoint=ctx.control.checkpoint, replan=replan,
                                   max_replans=int(conv.get("max_replans", 1)))
            best = res["best"]
            if not best.get("ok"):
                rec["status"] = "failed"
                ctx.save()
                raise RuntimeError(f"{scene['name']} 初稿构建失败：{best.get('error')}")
            built = await hub.call("blender_build_oskar", {"program": best["program"]})
            exp = await hub.call("blender_export_glb", {"asset_name": f"atlas_{sid}"})
            if not exp.get("ok"):
                raise RuntimeError(f"{scene['name']} 导出失败：{exp.get('error') or exp.get('problems')}")
            (out / "program_best.json").write_text(json.dumps(best["program"], ensure_ascii=False, indent=1))
            first = rec["history"][0] if rec["history"] else {}
            rec.update(status="done", best_round=best["round"], program=best["program"], director=best.get("director"),
                       scores=(best.get("critique") or {}).get("scores"), verdict=(best.get("critique") or {}).get("verdict"),
                       metrics=best.get("metrics") or {}, checks=best.get("checks"), checks_ok=best.get("checks_ok"),
                       stop_reason=res["stop_reason"], forecast=res["forecast"], start_director=first.get("director"),
                       converged=res["stop_reason"] == "converged", glb=exp["path"],
                       export_size=exp.get("info", {}).get("size") or built.get("size"),
                       walk=built.get("walk") or [], paths=built.get("paths") or [], images=best.get("images") or [])
            models[sid] = {"glb": exp["path"], "final_score": best.get("director"),
                           "rounds": [{"round": h["round"], "score": h.get("director"), "accepted": h.get("accepted")}
                                      for h in rec["history"]]}
            entry = {"scene": scene["name"], **res["lessons"], "note": rec.get("verdict") or "",
                     "stop": res["stop_reason"]}
            atlas.setdefault("journal", []).append(entry)
            rec["lessons"] = res["lessons"]
            ctx.artifact("glb", exp["path"], f"{scene['name']}.glb", asset=sid, score=best.get("director"),
                         triangles=exp.get("info", {}).get("triangles"), size=exp.get("info", {}).get("size"))
            ctx.artifact("layout", out / "program_best.json", f"最佳场景程序（{scene['name']}）")
            ctx.save()
            m = rec["metrics"]
            ctx.emit("result", f"{name} · {scene['name']}：{D.STOP_REASONS.get(res['stop_reason'], res['stop_reason'])}；"
                               f"最佳第 {best['round']} 轮，导演分 {first.get('director') or 0:.1f} → {best.get('director') or 0:.1f}"
                               + (f"，DINO {m.get('dino', 0):.3f}" if m else ""), {"scene": sid, "stop": res["stop_reason"]})
    try:   # 手记跟着 Agent 走：下一个世界开工时会读到
        style_agents.save_journal(ctx.options.get("style_agent", "oskar"), ctx.id, atlas.get("journal") or [])
        ctx.emit("info", f"{name} 把 {len(atlas.get('journal') or [])} 站的经验写进工作室手记（下一次创作会读到；风格规则仍需审核）")
    except (ValueError, OSError) as e:
        ctx.emit("warn", f"工作室手记未保存：{e}")


# ───────────────────────────── 4. 世界整合 ─────────────────────────────

async def _consistency(recs: dict[str, dict], order: list[str]) -> dict | None:
    """客观风格一致性：各场景最佳渲染两两 SigLIP 相似度（同一世界应彼此接近）。"""
    pairs = []
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            ia, ib = (recs[a].get("images") or [None])[0], (recs[b].get("images") or [None])[0]
            if not ia or not ib:
                continue
            m = await vision.objective_similarity(ia, [ib])
            if m:
                pairs.append({"a": a, "b": b, "siglip": round(m["siglip"], 3), "dino": round(m["dino"], 3)})
    if not pairs:
        return None
    low = min(pairs, key=lambda p: p["siglip"])
    return {"pairs": pairs, "mean": round(sum(p["siglip"] for p in pairs) / len(pairs), 3), "min": low}


def _journey(bible: dict, recs: dict[str, dict], lay: list[dict]) -> tuple[list, list, list]:
    """各场景的可站立点 / 通路换到整合后的世界坐标（Blender，Z 向上）；每站的入口与出口供玩法使用。"""
    walk, paths, stops = [], [], []
    names = {s["id"]: s for s in bible["scenes"]}
    for item in lay:
        r = recs[item["id"]]
        w = D.transform_points(r.get("walk") or [], item["at"], item["rot_z"])
        ps = [D.transform_points(p, item["at"], item["rot_z"]) for p in r.get("paths") or [] if p]
        walk += w
        paths += ps
        entry = ps[0][0] if ps else (w[0] if w else item["at"])
        exit_ = ps[-1][-1] if ps else (max(w, key=lambda p: math.dist(p, entry)) if w else item["at"])
        s = names[item["id"]]
        stops.append({"id": item["id"], "name": s["name"], "role": s["role"], "link_to_next": s.get("link_to_next"),
                      "center": item["at"], "entry": entry, "exit": exit_})
    return walk, paths, stops


async def compose(ctx) -> None:
    from .pipeline import route
    atlas = ctx.state["atlas"]
    atlas["bible"] = D.coerce_text(atlas["bible"])
    bible, direction = atlas["bible"], _direction(ctx)
    integ = direction.get("integration") or {}
    recs = atlas["scenes"]
    ids = [s["id"] for s in bible["scenes"]]
    missing = [sid for sid in ids if not (recs.get(sid) or {}).get("glb")]
    if missing:
        raise RuntimeError(f"以下场景尚未完成，不能整合：{'、'.join(missing)}")
    sizes = {sid: recs[sid].get("export_size") or [30, 30, 10] for sid in ids}
    programs = {sid: recs[sid]["program"] for sid in ids}
    checks = D.world_checks(programs, bible, palette_locked=bool(atlas.get("palette")))
    targets = {sid: (atlas.get("targets") or {}).get(sid, {}).get("path") or recs[sid].get("target") for sid in ids}
    ctx.emit("info", "计算各场景之间的客观风格一致性…")
    consistency = await _consistency(recs, ids)
    obj_txt = (f"客观风格一致性（各场景最佳渲染两两 SigLIP）：均值 {consistency['mean']}，"
               f"最低 {consistency['min']['siglip']}（{consistency['min']['a']} ↔ {consistency['min']['b']}）"
               if consistency else "客观风格一致性：相似度服务不可用，未核对")
    rounds = max(1, int(ctx.opt("atlas_compose_rounds", integ.get("compose_rounds", 2))))
    state = {"order": list(ids), "spacing": 1.0, "rotations": {}, "nudges": {}}
    # 世界形态由 Oskar 在世界圣经里决定（world_form），任务选项可覆盖；默认是一颗小行星
    form = ctx.opt("atlas_form", "") or bible.get("world_form") or integ.get("form", "planet")
    form = form if form in ("planet", "archipelago") else "planet"
    history, best = [], None
    name = _agent_name(ctx)
    world_pass = float(direction.get("world_pass", 6.5))
    dims = D.rubric(direction, "world_rubric")
    async with MCPHub(ctx.dir, servers=["blender"]) as hub:
        link_cache: dict[str, dict] = {}
        shared = bible.get("shared") or {}

        async def plan_terrain_links(st: dict, lay: dict) -> list[dict]:
            """Oskar 亲自协调相邻两站之间的地势：读两端岛缘 → 选地势 → 用同一个引擎生成连接地形。"""
            specs = PL.link_specs(lay, {sid: recs[sid].get("walk") or [] for sid in ids},
                                  level_h=float(shared.get("level_h", 2.0)), sea_level=int(shared.get("sea_level", 1)))
            if not specs:
                return []
            if "link_plan" not in st:
                tool(ctx, "LLM.plan_links", f"{name} 读 {len(specs)} 段连接两端的岛缘高度与间隙，为每段选地势（阶梯台地 / 山脊垭口 / 低地地峡）")
                try:
                    st["link_plan"] = await D.plan_links(specs, bible=bible, direction=direction, model=route(ctx, "plan"),
                                                         style_rules=_rules(ctx, "oskar"), on_delta=ctx.delta_emitter())
                except Exception as e:   # noqa: BLE001  LLM 不可用时按地势规则连
                    ctx.emit("warn", f"地势连接规划未拿到 {name} 的判断（{str(e)[:80]}），按岛缘高度规则选地势")
                    st["link_plan"] = {sp["id"]: {**PL.default_link(sp), "why": "规则：按两端岛缘高度"} for sp in specs}
                for sp in specs:
                    c = st["link_plan"].get(sp["id"]) or {}
                    ctx.emit("info", f"{name} · 连接 {sp['a']} → {sp['b']}：{PL.LINK_KINDS.get(c.get('kind'), c.get('kind'))}，"
                                     f"宽 {c.get('width', 11):g} 米（两端第 {sp['h_a']} / {sp['h_b']} 层，间隙 {sp['gap']:.0f} 米）"
                                     + (f" — {c['why']}" if c.get("why") else ""))
            out = []
            for i, sp in enumerate(specs):
                choice = {**(st["link_plan"].get(sp["id"]) or PL.default_link(sp)),
                          **((st.get("link_overrides") or {}).get(sp["id"]) or {})}
                pa = programs.get(sp["a"]) or {}
                prog = PL.link_program(sp, choice, palette=pa.get("palette"), palette_base=pa.get("palette_base"),
                                       vegetation=pa.get("vegetation"), seed=i + 1)
                key = hashlib.sha1(json.dumps(prog, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]
                got = link_cache.get(key)
                if got is None:
                    tool(ctx, "Blender.build_oskar", f"生成连接地形 {sp['a']} → {sp['b']}（{PL.LINK_KINDS[prog['_link']['kind']]}）")
                    b = await hub.call("blender_build_oskar", {"program": prog})
                    if b.get("ok") is False:
                        raise RuntimeError(f"连接地形 {sp['id']} 构建失败：{b.get('error')}")
                    e = await hub.call("blender_export_glb", {"asset_name": f"atlas_{sp['id']}_{key[:6]}"})
                    if not e.get("ok"):
                        raise RuntimeError(f"连接地形 {sp['id']} 导出失败：{e.get('error') or e.get('problems')}")
                    got = {"glb": e["path"], "walk": b.get("walk") or [], "paths": b.get("paths") or [],
                           "triangles": e.get("info", {}).get("triangles")}
                    link_cache[key] = got
                out.append({**sp, **prog["_link"], "why": choice.get("why", ""), **got})
            return out

        async def place_planet(st: dict, tag: str):
            lay = PL.plan(st["order"], sizes, climax=atlas["anchor"], gap=float(integ.get("planet_gap_m", 6.0)),
                          spacing=st["spacing"], rotations=st["rotations"], nudges=st["nudges"])
            st["ring"] = len(lay["links"]) >= len(st["order"]) > 2
            terrain = await plan_terrain_links(st, lay)
            lay["terrain_links"] = terrain
            kinds = "、".join(f"{PL.LINK_KINDS[k]}×{n}" for k, n in
                             sorted(collections.Counter(t["kind"] for t in terrain).items())) or "无"
            tool(ctx, "Blender.compose_planet", f"把 {len(lay['scenes'])} 个场景与 {len(terrain)} 段地势连接（{kinds}）"
                                                f"贴到半径 {lay['radius']:.0f} 米的星球上")
            items = [{"id": x["id"], "glb": recs[x["id"]]["glb"], "lat": x["lat"], "lon": x["lon"], "yaw": x["yaw"], "r": x["r"]}
                     for x in lay["scenes"]]
            items += [{"id": t["id"], "glb": t["glb"], "lat": t["lat"], "lon": t["lon"], "yaw": t["yaw"],
                       "r": max(4.0, t["gap"] / 2), "kind": "link"} for t in terrain]
            comp = await hub.call("blender_compose_planet", {"items": items, "radius": lay["radius"], "links": []})
            if comp.get("errors") or comp.get("ok") is False:
                raise RuntimeError("星球整合失败：" + "；".join((comp.get("errors") or ["未知错误"])[:3]))
            r = await hub.call("blender_render", {"views": ["hero", "hero_left"], "label": f"atlas_{tag}", "lighting": "day"})
            overview, faces = r.get("images", []), []
            for sc in lay["scenes"][:5]:   # 依次把每一站转到镜头前，像在星球表面看它
                M = PL.face_rotation(sc)
                await hub.call("blender_run_python", {"code": (
                    f"from mathutils import Matrix\nbpy.data.objects['oskar_planet'].matrix_world = Matrix({M}).to_4x4()\n"
                    f"for o in bpy.data.objects:\n    o['ignore_bounds'] = not (o.parent and o.parent.name == '{sc['id']}')\n"
                    "bpy.context.view_layer.update()")})
                fr = await hub.call("blender_render", {"views": ["hero"], "label": f"atlas_{tag}_{sc['id']}", "lighting": "day"})
                faces += fr.get("images", [])
            await hub.call("blender_run_python", {"code": (
                "from mathutils import Matrix\nbpy.data.objects['oskar_planet'].matrix_world = Matrix.Identity(4)\n"
                "for o in bpy.data.objects:\n    o['ignore_bounds'] = False\nbpy.context.view_layer.update()")})
            return lay, comp, overview, faces

        async def place(st: dict, tag: str):
            if form == "planet":
                return await place_planet(st, tag)
            lay = D.layout(st["order"], sizes, climax=atlas["anchor"], gap=float(integ.get("gap_m", 8.0)),
                           spacing=st["spacing"], rotations=st["rotations"], nudges=st["nudges"])
            tool(ctx, "Blender.compose_scene", f"按旅程弧线摆放 {len(lay)} 个场景（间距 ×{st['spacing']}）")
            comp = await hub.call("blender_compose_scene", {"items": [{"id": x["id"], "glb": recs[x["id"]]["glb"],
                                                                        "at": x["at"], "rot_z": x["rot_z"]} for x in lay]})
            if comp.get("errors"):
                raise RuntimeError("整合导入失败：" + "；".join(comp["errors"][:3]))
            r = await hub.call("blender_render", {"views": ["hero", "hero_left"], "label": f"atlas_{tag}", "lighting": "day"})
            return lay, comp, r.get("images", []), []

        for rnd in range(rounds):
            await ctx.control.checkpoint()
            lay, comp, imgs, faces = await place(state, f"r{rnd}")
            for p in imgs + faces:
                ctx.artifact("render", p, f"世界整合 第{rnd}轮", asset="_atlas", round=rnd)
            tool(ctx, "VLM.world_review", "审看整合全景：色彩统一、节奏、尺度、衔接" + ("（星球全景 + 每一站贴在球面上的样子）" if faces else ""))
            review = await D.world_review(imgs + faces[:4], targets, bible=bible, order=state["order"], checks=checks,
                                          objective=obj_txt, direction=direction, model=route(ctx, "observe"),
                                          style_rules=_rules(ctx, "review"), on_delta=ctx.delta_emitter(), form=form)
            rec = {"round": rnd, "state": state, "layout": lay, "images": imgs, "faces": faces, "review": review,
                   "size": comp.get("size"), "form": form}
            accepted = best is None or (review["director"] or 0) >= (best["review"]["director"] or 0) + 0.3
            rec["accepted"] = accepted
            history.append(rec)
            if accepted:
                best = rec
            ctx.emit("review", f"{name} · 世界整合第 {rnd} 轮：导演分 {review['director']:.1f}（{_dims_brief(review['scores'], dims)}）"
                               f"{'，采用' if accepted else '，未提升，保留上一版排布'} — {review['verdict']}",
                     {"round": rnd, **review})
            atlas["world"] = {"history": history, "best_round": best["round"], "checks": checks, "consistency": consistency}
            ctx.save()
            if rnd + 1 >= rounds or not best["review"]["layout_ops"]:
                break
            state, log = (PL.apply_ops if form == "planet" else D.apply_layout_ops)(best["state"], best["review"]["layout_ops"], ids)
            ctx.emit("info", f"{name} 调整排布：{'；'.join(log) or '无有效操作'}")
        if best is not history[-1]:
            await place(best["state"], "best")
        exp = await hub.call("blender_export_glb", {"asset_name": "oskar_atlas"})
    if not exp.get("ok"):
        raise RuntimeError(f"世界导出失败：{exp.get('error') or exp.get('problems')}")
    if form == "planet":
        walk, paths, stops = PL.journey(recs, best["layout"], {s["id"]: s for s in bible["scenes"]},
                                        links=best["layout"].get("terrain_links") or [])
    else:
        walk, paths, stops = _journey(bible, recs, best["layout"])
    review = best["review"]
    atlas["world"] = {"history": history, "best_round": best["round"], "checks": checks, "consistency": consistency,
                      "form": form, "faces": best.get("faces") or [],
                      "planet": {**{k: best["layout"][k] for k in ("radius", "scenes", "links")},
                                 "terrain_links": [{k: v for k, v in t.items() if k not in ("walk", "paths")}
                                                   for t in best["layout"].get("terrain_links") or []]} if form == "planet" else None,
                      "layout": best["layout"], "order": best["state"]["order"], "review": review,
                      "director": review["director"], "passed": (review["director"] or 0) >= world_pass,
                      "world_pass": world_pass, "glb": exp["path"], "export_size": exp.get("info", {}).get("size"),
                      "images": best["images"], "walk": walk, "paths": paths, "journey": stops}
    out = ctx.dir / "atlas"
    (out / "world_layout.json").write_text(json.dumps({"layout": best["layout"], "journey": stops}, ensure_ascii=False, indent=1))
    ctx.artifact("glb", exp["path"], f"{bible['title']} · 整合世界.glb", asset="oskar_atlas", score=review["director"],
                 triangles=exp.get("info", {}).get("triangles"), size=exp.get("info", {}).get("size"))
    ctx.artifact("layout", out / "world_layout.json", "世界排布与旅程 world_layout.json")
    ctx.save()
    fails = [c["name"] for c in checks if c["required"] and not c["passed"]]
    ctx.emit("result", f"{name} · 世界整合：导演分 {review['director']:.1f}（通过线 {world_pass:g}）"
                       + (f"；未通过：{'、'.join(fails)}" if fails else "；尺度与色彩检查通过")
                       + f"；可站立点 {len(walk)} 个，{len(stops)} 站旅程"
                       + (f"；星球半径 {best['layout']['radius']:.0f} 米，{len(best['layout'].get('terrain_links') or [])} 段地势连接"
                          f"（{'、'.join(PL.LINK_KINDS.get(t['kind'], t['kind']) for t in best['layout'].get('terrain_links') or []) or '无'}）"
                          if form == "planet" else ""))


# ───────────────────────────── 5. Godot ─────────────────────────────

async def godot(ctx) -> None:
    from .pipeline import _godot_project
    from .blender import look
    from .godot import oskar_scene as OG
    world = ctx.state["atlas"]["world"]
    imgs = world.get("images") or []
    views = {("hero_left" if "hero_left" in p else "hero"): p for p in imgs}
    journey = [{**s, **{k: look.to_godot(s[k]) for k in ("center", "entry", "exit")}} for s in world.get("journey") or []]
    extra = {"journey": journey}
    if world.get("form") == "planet" and world.get("planet"):
        # 星球：云绕球流动；海洋球不做碰撞
        extra["planet"] = {"center": [0.0, 0.0, 0.0], "radius": world["planet"]["radius"]}
        extra["no_collision"] = ["oskar_sea", "oskar_props", "oskar_ocean"]
        extra["clouds"] = {"count": 14, "speed": 1.2, "wind": [1.0, 0.0, 0.35], "in_showcase": False}
        # 星球玩法：小老虎探险、每站一封信、环球有轨电车（与“游戏组装 · 星球”同一套）
        atlas = ctx.state["atlas"]
        names = {s["id"]: s for s in atlas["bible"]["scenes"]}
        g = PL.game_layout(atlas["scenes"], world["layout"], names)
        extra["game"] = {"letters": [{**L, "pos": look.to_godot(L["pos"])} for L in g["letters"]],
                         "fog_density": g.get("fog_density", 0.0),
                         "tram": {**g["tram"], "route": [look.to_godot(u) for u in g["tram"]["route"]]}}
    tool(ctx, "Godot.import + run", "把整合世界接入 Godot：碰撞、相机、光照，试运行截图并与 Blender 核对")
    async with MCPHub(ctx.dir, godot_project=_godot_project(ctx), servers=["godot"]) as hub:
        res = await OG.setup_and_verify(hub, _godot_project(ctx), blender_views=views or None,
                                        blender_size=world.get("export_size"), walk=world.get("walk"),
                                        paths=world.get("paths"), emit=_say(ctx), glb_name="oskar_atlas.glb",
                                        extra=extra,
                                        main_gd=planet_assembly.PLANET_MAIN_GD if "game" in extra else None)
    for p, label in zip(res["shots"], res["labels"]):
        ctx.artifact("screenshot", p, f"Godot · 整合世界 · {label}", asset="oskar_atlas", shot=label)
    ctx.state["atlas"]["godot"] = {k: res[k] for k in ("ok", "checks", "shots", "labels", "match", "fps", "project")}
    ctx.save()
    fails = [c for c in res["checks"] if c["required"] and not c["passed"]]
    ctx.emit("review", "Godot 接入（整合世界）：" + ("全部核对通过" if not fails else "未通过：" + "；".join(
        f"{c['name']}（{c['detail']}）" for c in fails)) + (f"；同机位相似度 {res['match']}" if res["match"] else ""),
        {"checks": res["checks"], "match": res["match"]})
    if fails:
        raise RuntimeError("Godot 接入核对未通过：" + "；".join(f"{c['name']} {c['detail']}" for c in fails))


# ───────────────────────────── 交付报告 ─────────────────────────────

def report_lines(state: dict) -> list[str]:
    atlas = state.get("atlas") or {}
    bible = atlas.get("bible") or {}
    direction = atlas.get("direction") or {}
    dims = D.rubric(direction)
    lines = ["", f"## {atlas.get('agent', 'Oskar')} 导演的世界", "", f"> {bible.get('vision', '')}", ""]
    lines += [f"- 支柱：{p}" for p in bible.get("pillars", [])]
    lines += [f"- 色彩角色：{(bible.get('palette') or {}).get('notes', '-')}",
              f"- 共享尺度：格网 {bible.get('shared', {}).get('cell')} m、层高 {bible.get('shared', {}).get('level_h')} m、"
              f"海面第 {bible.get('shared', {}).get('sea_level')} 层", ""]
    lines += ["### 逐场景趋近", "", "| 站 | 角色 | 目标图导演分 | 导演分 起→终 | DINO | 最佳轮 | 结束原因 |", "|---|---|---|---|---|---|---|"]
    for s in bible.get("scenes", []):
        r = (atlas.get("scenes") or {}).get(s["id"], {})
        t = (atlas.get("targets") or {}).get(s["id"], {})
        lines.append(f"| {s['name']} | {D.ROLES.get(s['role'], s['role'])} | {t.get('director', '-')}"
                     f"{'（人工改选）' if t.get('human_override') else ''} | {r.get('start_director', '-')} → {r.get('director', '-')} | "
                     f"{(r.get('metrics') or {}).get('dino', '-')} | {r.get('best_round', '-')} | "
                     f"{D.STOP_REASONS.get(r.get('stop_reason'), r.get('stop_reason', '-'))} |")
    for s in bible.get("scenes", []):
        r = (atlas.get("scenes") or {}).get(s["id"], {})
        if r.get("scores"):
            lines.append(f"- {s['name']}：{_dims_brief(r['scores'], dims)} — {r.get('verdict', '')}")
    w = atlas.get("world") or {}
    if w:
        rv = w.get("review") or {}
        lines += ["", "### 世界整合", "", f"- 导演分 {rv.get('director', '-')}（通过线 {w.get('world_pass', '-')}）：{rv.get('verdict', '')}",
                  f"- 世界维度：{_dims_brief(rv.get('scores'), D.rubric(direction, 'world_rubric'))}",
                  f"- 检查：{'；'.join(c['name'] + ('通过' if c['passed'] else '未通过') for c in w.get('checks', []))}"]
        if w.get("consistency"):
            lines.append(f"- 客观风格一致性（两两 SigLIP）：均值 {w['consistency']['mean']}，最低 {w['consistency']['min']['siglip']}")
        if w.get("form") == "planet" and w.get("planet"):
            pl = w["planet"]
            tl = pl.get("terrain_links") or []
            lines.append(f"- 形态：小行星，海平面半径 {pl['radius']:.0f} 米，{len(tl)} 段地势连接"
                         f"（{'、'.join(f"{t['a']}→{t['b']} {PL.LINK_KINDS.get(t['kind'], t['kind'])}" for t in tl) or '无'}）；"
                         + "，".join(f"{s['id']} 纬 {s['lat']:.0f}° 经 {s['lon']:.0f}°" for s in pl["scenes"]))
        lines.append(f"- 旅程：{' → '.join(x['name'] for x in w.get('journey', []))}（各站入口 / 出口写在 godot_project/oskar_scene.json 的 journey）")
        for sid, note in (rv.get("scene_notes") or {}).items():
            lines.append(f"- 下次迭代 · {sid}：{note}")
    lines += ["", "边界：星球形态下各站之间由 Oskar 规划的地势连接（引擎生成，可碰撞），但角色沿球面行走（指向球心的重力）尚未实现；群岛形态下场景之间没有连接；"
              "导演分是 VLM 依锚点的主观评分，接受规则以硬检查与客观相似度为先；静帧不证明通行与动画。"]
    return lines
