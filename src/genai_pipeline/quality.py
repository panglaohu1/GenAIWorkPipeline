"""Read-only quality assessment shared by delivery and the workbench.

Execution status and package existence are deliberately not acceptance evidence.
This checks the recorded baseline, not unimplemented end-to-end gameplay tests.
"""
from __future__ import annotations

import math

from . import config


def assess(run: dict, state: dict) -> dict:
    checks: list[dict] = []
    mode = run.get("mode", "asset")
    options = run.get("options") or {}
    if mode == 'consult':
        ready = bool(state.get('consultation'))
        label = {'queued':'待审阅','preparing':'准备审阅资料','failed':'审阅失败','cancelled':'审阅已取消',
                 'interrupted':'审阅已中断','paused':'审阅已暂停'}.get(run.get('status'),'审阅中')
        return {'status':'unverified','label':'提案待审核' if ready else label,'checks':[],'issues':[],
                'failed_count':0,'unknown_count':1,'suggested_stage':None,
                'scope':'顾问提案尚未执行，不代表作品已修改或验收通过。'}
    if mode == "learn":
        ready = bool(state.get("learning"))
        label = {"queued": "待研究", "preparing": "待研究", "paused": "研究已暂停", "failed": "研究失败",
                 "cancelled": "研究已取消", "interrupted": "研究已中断"}.get(run.get("status"), "研究中")
        return {"status": "unverified", "label": "草稿待审核" if ready else label, "checks": [], "issues": [],
                "failed_count": 0, "unknown_count": 1, "suggested_stage": None,
                "scope": "学习产出为风格规则草稿；需在系统 Agent 页面审核应用，不会自动改变已运行任务。"}
    threshold = float(options.get("model_pass_score") or config.get("pipeline.model_pass_score", 7.5))

    def add(name, passed, stage, detail="", asset=None):
        checks.append({"name": name, "passed": passed, "stage": stage, "detail": detail, "asset": asset})

    def hard(records, stage, prefix):
        required = [c for c in records or [] if c.get("required")]
        if not required:
            add(prefix, None, stage, "没有必需检查记录")
        for c in required:
            add(f"{prefix} · {c.get('name', '检查')}", c.get("passed"), stage, c.get("detail", ""))

    def score(value, minimum):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= minimum


    planet = mode == "assembly" and (options.get("assembly") or {}).get("form") == "planet"
    if planet:
        pl = state.get("planet") or {}
        add("星球整合 GLB 导出", True if pl.get("glb") else None, "planet_compose")
        add("每站一封信与电车线路", (bool(pl["game"]["letters"]) and len(pl["game"]["tram"]["route"]) > 3) if pl.get("game") else None,
            "planet_compose", f"{len((pl.get('game') or {}).get('letters') or [])} 封信")
        g = pl.get("godot") or {}
        add("Godot 星球游戏接入", g.get("ok"), "planet_godot")
        hard(g.get("checks"), "planet_godot", "Godot")
    elif mode in ("game", "assembly"):
        fr = state.get("final_review") or {}
        test, review = fr.get("test") or {}, fr.get("review") or {}
        add("游戏运行硬检查", test.get("hard_ok"), "assemble", test.get("facts", "尚未完成试玩"))
        add("游戏视觉验收", score(review.get("score"), threshold) and review.get("pass") is True if review else None,
            "assemble", f"{review.get('score', '—')} / 10，通过线 {threshold:g}")
        if state.get("world"):
            hard(state["world"].get("checks"), "world", "世界硬检查")

    if mode == "assembly":
        manifest = options.get("assembly") or {}
        parts = manifest.get("parts") or []
        add("组装来源验收快照", bool(parts) and all(p.get("quality", {}).get("status") == "passed" for p in parts),
            "assembly_plan", f"{len(parts)} 条已选产线；保留原始验收，不代表整体游戏通过")
        add("组装输入完整性", (state.get("assembly") or {}).get("sources_verified"), "assembly_plan")
        add("完整关卡流程人工试玩", (state.get("assembly") or {}).get("journey_approved") or None, "journey",
            "需确认每关目标、出口、关卡切换、通关与重新开始")
    elif mode == "atlas":
        atlas = state.get("atlas") or {}
        bible = atlas.get("bible") or {}
        conv = (atlas.get("direction") or {}).get("convergence") or {}
        scene_pass = float(options.get("atlas_scene_pass") or conv.get("scene_pass", 7.0))
        target_pass = float(conv.get("target_pass", 6.5))
        min_dino = float(config.get("metrics.pass_min_dino", 0.25))
        if not bible.get("scenes"):
            add("世界圣经", None, "atlas_brief", "尚未构想世界")
        for scene in bible.get("scenes") or []:
            sid, label = scene["id"], scene.get("name", scene["id"])
            t = (atlas.get("targets") or {}).get(sid) or {}
            td = t.get("director")
            # 导演认可或人工确认即可；都没有时是“待验证”（需要人看），不是失败
            target_ok = (score(td, target_pass) and t.get("buildable", True)) or bool(t.get("human_confirmed"))
            add(f"{label} · 目标图导演评审", True if t and target_ok else None, "atlas_targets",
                f"导演分 {td if td is not None else '—'}，目标 {target_pass:g}" + ("；已人工确认" if t.get("human_confirmed") else ""), sid)
            r = (atlas.get("scenes") or {}).get(sid) or {}
            add(f"{label} · GLB 导出", True if r.get("glb") else None, "atlas_scenes", asset=sid)
            hard(r.get("checks"), "atlas_scenes", f"{label} · 硬检查")
            add(f"{label} · 导演趋近评分", score(r.get("director"), scene_pass) if r else None, "atlas_scenes",
                f"{r.get('start_director', '—')} → {r.get('director', '—')}，通过线 {scene_pass:g}；"
                f"{r.get('stop_reason', '未开始')}", sid)
            dino = (r.get("metrics") or {}).get("dino")
            add(f"{label} · 结构相似度", score(dino, min_dino) if dino is not None else None, "atlas_scenes",
                f"DINO {dino if dino is not None else '未核对'}，下限 {min_dino:g}", sid)
        world = atlas.get("world") or {}
        hard(world.get("checks"), "atlas_compose", "世界检查")
        add("世界整合导演评审", world.get("passed") if world.get("review") else None, "atlas_compose",
            f"{world.get('director', '—')} / 10，通过线 {world.get('world_pass', '—')}")
        add("整合世界 GLB 导出", True if world.get("glb") else None, "atlas_compose")
        godot = atlas.get("godot") or {}
        add("Godot 世界接入", godot.get("ok"), "atlas_godot")
        hard(godot.get("checks"), "atlas_godot", "Godot")
        if not godot.get("match"):
            add("跨引擎画面一致性", None, "atlas_godot", "缺少相似度核对数据")
    elif mode == "oskar":
        oskar = state.get("oskar") or {}
        hard(oskar.get("checks"), "oskar_build", "场景硬检查")
        exported = oskar.get("glb") or ((state.get("models") or {}).get("oskar_scene") or {}).get("glb")
        add("场景 GLB 导出", True if exported else None, "oskar_build", "检查导出记录")
        godot = oskar.get("godot") or {}
        add("Godot 接入验收", godot.get("ok"), "oskar_godot")
        hard(godot.get("checks"), "oskar_godot", "Godot")
        if not godot.get("match"):
            add("跨引擎画面一致性", None, "oskar_godot", "缺少相似度核对数据")
    else:
        assets = (state.get("spec") or {}).get("assets") or []
        if not assets:
            add("资产清单", None, "plan" if mode not in ("scene", "scene3d") else "decompose", "尚无资产清单")
        for asset in assets:
            aid, label = asset["id"], asset.get("name", asset["id"])
            model = (state.get("models") or {}).get(aid) or {}
            rounds = model.get("rounds") or []
            exported = model.get("exported_round")
            selected = next((r for r in rounds if exported is not None and r.get("round") == exported), None)
            if selected is None and exported is None:
                index = model.get("best")
                if isinstance(index, int) and 0 <= index < len(rounds):
                    selected = rounds[index]
            if model.get("mode") == "skip":
                continue
            add(f"{label} · GLB 导出", True if model.get("glb") else None, "model", asset=aid)
            if model.get("mode") == "terrain":   # Oskar 引擎地形：引擎硬检查
                add(f"{label} · 地形硬检查（Oskar 引擎）", model.get("checks_ok") if model.get("checks") is not None else None,
                    "model", "；".join(f"{c['name']} {c.get('detail', '')}" for c in (model.get("checks") or [])
                                      if c.get("required") and not c.get("passed")), aid)
                continue
            if model.get("mode") == "skip":   # 天空 / 太阳：不建模
                continue
            if model.get("mode") == "backdrop":   # 远景层：只要求导出成功
                continue
            if model.get("mode") == "engine":   # Oskar 引擎构件：确定性几何，构建 + 导出成功即可；像不像由场景组装验收
                continue
            if model.get("mode") == "image3d":   # 图生 3D：多稿择优（客观相似度选候选），台账分只作记录
                obs = model.get("observation") or {}
                cands = model.get("candidates") or []
                bi = model.get("best")
                best = cands[bi] if isinstance(bi, int) and 0 <= bi < len(cands) else {}
                met = (best.get("metrics") or {}) if best else {}
                img_thr = threshold
                by_ledger = score(obs.get("score"), img_thr) if obs else None
                # 客观相似度只作记录，不放行（2026-10-30 用户确认：门槛不因客观分放宽）
                passed = by_ledger
                how = "台账" if by_ledger else "未达"
                obj = f"；DINO {met['dino']:.3f} / SigLIP {met['siglip']:.3f}" if met.get("dino") is not None else ""
                add(f"{label} · 视觉验收（图生 3D）", passed, "model",
                    f"{len(cands)} 个候选，选用候选 {best.get('k', '—') if best else '—'}"
                    f" · {obs.get('score', '—')} / 10，图生通过线 {img_thr:g}（{how}）{obj}", aid)
                continue
            if selected is None:
                add(f"{label} · 版本验收", None, "model", "缺少导出版本的验收证据", aid)
                continue
            review = selected.get("review") or {}
            value = selected.get("score")
            met = selected.get("metrics") or {}
            by_ledger = score(value, threshold) and review.get("pass") is True
            vis_ok = by_ledger
            how = "台账" if by_ledger else "未达"
            obj = f"；DINO {met['dino']:.3f} / SigLIP {met['siglip']:.3f}" if met.get("dino") is not None else ""
            add(f"{label} · 视觉验收", vis_ok if selected else None,
                "model", f"第 {selected.get('round')} 轮 · {value} / 10，通过线 {threshold:g}（{how}）{obj}", aid)
            problems = selected.get("hard")
            if isinstance(problems, list):
                # 按当前 poly_budget 重算三角面项：规划常给角色 4000，蓝图耳朵/腿一拼就 6k+
                budget = int(asset.get("poly_budget") or 0)
                if budget > 0:
                    rebuilt = []
                    for p in problems:
                        s = str(p)
                        if "三角面" in s and "超过预算" in s:
                            try:
                                tris = int("".join(ch for ch in s.split("三角面", 1)[1].split("超过", 1)[0] if ch.isdigit()))
                                if tris <= budget * 1.2:
                                    continue
                                rebuilt.append(f"三角面 {tris} 超过预算 {budget}")
                            except ValueError:
                                rebuilt.append(s)
                        else:
                            rebuilt.append(s)
                    problems = rebuilt
            add(f"{label} · 硬检查", not problems if isinstance(problems, list) else None,
                "model", "；".join(str(p) for p in problems or []), aid)
        if mode in ("scene", "scene3d"):
            from . import jev
            comp = state.get("compose") or {}
            rounds, champion = comp.get("rounds") or [], comp.get("champion")
            selected = rounds[champion] if isinstance(champion, int) and 0 <= champion < len(rounds) else None
            minimum = float(options.get("deliver_min_score") or config.get("pipeline.deliver_min_score", 6))
            value = None
            detail = f"通过线 {minimum:g} / 10"
            passed = None
            if selected:
                diffs = ((selected.get("observation") or {}).get("diffs")) or []
                # 用当前公式重算：旧运行在饱和公式下会存成 0，与组装冠军实际选用的客观分脱节
                value = jev.ledger_score(diffs) if diffs else selected.get("score")
                m = selected.get("metrics") or {}
                by_ledger = score(value, minimum)
                passed = by_ledger
                how = "台账" if by_ledger else "未达"
                obj = f"；DINO {m['dino']:.3f} / SigLIP {m['siglip']:.3f}" if m.get("dino") is not None else ""
                detail = f"第 {selected.get('round')} 轮 · {value} / 10，通过线 {minimum:g}（{how}）{obj}"
            add("场景组装验收", passed, "compose", detail)

    failed = [c for c in checks if c["passed"] is False]
    unknown = [c for c in checks if c["passed"] is not True and c["passed"] is not False]
    status = "blocked" if failed else "unverified" if unknown else "passed"
    issues = failed + unknown
    return {"status": status, "label": {"blocked": "验收未通过", "unverified": "待验证", "passed": "已记录检查通过"}[status],
            "checks": checks, "issues": issues, "failed_count": len(failed), "unknown_count": len(unknown),
            "suggested_stage": issues[0]["stage"] if issues else None,
            "scope": "基于当前保存的验收证据；尚不包含完整玩法、全地图可达性或人工美术验收。"}
