"""Oskar 导演的多场景世界：纯逻辑与注入式趋近循环（不需要 LLM / Blender / Godot）。"""
import copy

import pytest

from genai_pipeline import style_agents
from genai_pipeline.oskar import director as D
from genai_pipeline.quality import assess


def bible(n=3):
    roles = ["opening", "climax", "resolve", "rise", "interlude", "rise"][:n]
    if "climax" not in roles:
        roles[-1] = "climax"
    return {"title": "群岛", "vision": "先有海，再有城", "pillars": ["大形先行"], "art_style": "low poly",
            "palette": {"base": "citadel", "roles": "ivory stone", "notes": "象牙石为主"},
            "shared": {"sea_level": 1},
            "scenes": [{"id": f"s{i}", "name": f"第{i}站", "role": r, "intensity": 5 if r == "climax" else 2,
                        "dominant": "岩柱", "key_art_prompt": "an island"} for i, r in enumerate(roles)]}


DIR = D.resolve_direction(None)
DIMS = D.rubric(DIR)


def scores(v):
    return {d["id"]: v for d in DIMS}


def test_direction_lives_in_agent_definition_and_is_inherited(monkeypatch, tmp_path):
    monkeypatch.setattr(style_agents, "_root", lambda: tmp_path / "agents")
    oskar = style_agents.direction("oskar")
    assert {d["id"] for d in oskar["rubric"]} >= {"silhouette", "terraces", "world"}
    assert abs(sum(d["weight"] for d in oskar["rubric"]) - 1) < 1e-6
    custom = style_agents.create("暮色群岛", "黄昏世界", ["先确定岛屿剪影。"])
    snap = style_agents.snapshot(custom["id"])
    assert snap["direction"]["inherited_from"] == "oskar"
    assert snap["direction"]["rubric"] == oskar["rubric"]


def test_journal_is_saved_per_agent_and_read_back(monkeypatch, tmp_path):
    monkeypatch.setattr(style_agents, "_root", lambda: tmp_path / "agents")
    style_agents.save_journal("oskar", "run-a", [{"scene": "港", "ops": {"move": {"tried": 2, "accepted": 1}}}])
    prior = style_agents.recent_journals("oskar", exclude="run-b")
    assert prior[0]["run_id"] == "run-a"
    assert style_agents.recent_journals("oskar", exclude="run-a") == []
    text = D.journal_text(prior)
    assert "move 1/2" in text


def test_bible_validation_and_normalisation():
    b = bible()
    assert D.validate_bible(b) is None
    bad = copy.deepcopy(b)
    bad["scenes"][0]["intensity"] = 5
    bad["scenes"][1]["intensity"] = 3
    assert "climax" in D.validate_bible(bad)
    two = copy.deepcopy(b)
    two["scenes"][0]["role"] = "climax"
    assert "恰好一个" in D.validate_bible(two)
    n = D.normalize_bible(b)
    assert n["shared"] == {"sea_level": 1, "cell": 2.0, "level_h": 2.0}
    assert n["scenes"][1]["grid_radius"] == 9 and n["scenes"][-1]["link_to_next"] == "none"
    assert [s["id"] for s in D.target_order(n)][0] == "s1"   # 高潮先出图，成为世界母题


def test_style_lock_shared_by_every_scene():
    b = D.normalize_bible(bible())
    prompts = [D.target_prompt(b, s, DIR) for s in b["scenes"]]
    lock = DIR["target_style_lock"]
    assert all(p.endswith(lock) and "ivory stone" in p for p in prompts)


def test_lock_program_enforces_world_scale_and_palette():
    b = D.normalize_bible(bible())
    prog = {"grid": {"radius": 3, "cell": 5}, "level_h": 3, "sea_level": 0, "palette": "sea_stack", "items": []}
    p = D.lock_program(prog, b, b["scenes"][1], {"rock": "#111111"}, "citadel")
    assert p["grid"] == {"radius": 9, "cell": 2.0} and p["level_h"] == 2.0 and p["sea_level"] == 1
    assert p["palette"] == {"rock": "#111111"} and prog["palette"] == "sea_stack"


def test_phases_follow_oskar_order():
    assert D.phase_for(False, scores(9), DIR)["id"] == "fix"
    assert D.phase_for(True, None, DIR)["id"] == "form"
    s = scores(8)
    s["terraces"] = 4
    assert D.phase_for(True, s, DIR)["id"] == "tiers"
    s = scores(8)
    s["silhouette"] = 5
    s["terraces"] = 4
    assert D.phase_for(True, s, DIR)["id"] == "form"   # 大形没成立前不谈层次
    assert D.phase_for(True, scores(8), DIR)["id"] == "finish"


def test_acceptance_rules_put_evidence_first():
    conv = DIR["convergence"]
    best = {"ok": True, "checks_ok": True, "objective": 50, "director": 6}
    assert D.decide({"ok": True, "checks_ok": False, "objective": 60, "director": 9}, best, conv)[0] is False
    assert D.decide({"ok": True, "checks_ok": True, "objective": 51, "director": 6}, best, conv)[0] is True
    assert D.decide({"ok": True, "checks_ok": True, "objective": 51, "director": 4}, best, conv)[0] is False
    assert D.decide({"ok": True, "checks_ok": True, "objective": 50.1, "director": 7.2}, best, conv)[0] is True
    assert D.decide({"ok": True, "checks_ok": True, "objective": 49, "director": 9}, best, conv)[0] is False
    assert D.decide({"ok": True, "checks_ok": True, "objective": 40, "director": 2},
                    {**best, "checks_ok": False}, conv)[0] is True
    assert D.decide({"ok": False, "error": "x"}, best, conv)[0] is False


PROGRAM = {"seed": 1, "grid": {"radius": 5}, "items": [{"type": "stack", "at": [0, 0], "tiers": [{"r": 0.3, "top": 3}]}]}


def fake_tools(script):
    """script: 每次 evaluate 返回的 (checks_ok, objective) 与 critique 的 (director 分, action)。"""
    evals, crits = list(script["evals"]), list(script["crits"])
    calls = {"eval": [], "crit": [], "replan": 0}

    async def evaluate(pg, tag):
        ok, obj = evals.pop(0)
        calls["eval"].append(tag)
        return {"ok": True, "checks_ok": ok, "objective": obj, "metrics": {"dino": 0.5, "siglip": 0.8},
                "checks": [{"name": "无悬空", "required": True, "passed": ok, "detail": ""}], "images": []}

    async def critique(rec, phase, failed):
        v, action = crits.pop(0) if crits else (5, "edit")
        calls["crit"].append(phase["id"])
        return {"scores": scores(v), "verdict": "主峰再高一点", "action": action,
                "diffs": [{"what": "主峰太矮", "op": "top", "item": 0, "tier": -1, "delta": 1}]}

    async def replan(best, failed):
        calls["replan"] += 1
        return copy.deepcopy(PROGRAM)
    return evaluate, critique, replan, calls


async def test_converge_fixes_hard_checks_then_converges():
    ev, cr, rp, calls = fake_tools({"evals": [(False, 50), (True, 48), (True, 52)],
                                    "crits": [(4, "edit"), (6, "edit"), (7.5, "edit")]})
    res = await D.converge(PROGRAM, evaluate=ev, critique=cr, direction=DIR, max_rounds=4, min_dino=0.25, replan=rp)
    assert calls["crit"][0] == "fix"
    assert [h["accepted"] for h in res["history"]] == [True, True, True]
    assert res["stop_reason"] == "converged" and res["best"]["round"] == 2
    assert res["lessons"]["ops"]["top"] == {"tried": 2, "accepted": 2}
    assert res["forecast"]["gap"] == 0


async def test_plateau_triggers_one_replan_before_stopping():
    ev, cr, rp, calls = fake_tools({"evals": [(True, 50)] + [(True, 49)] * 6, "crits": [(5, "edit")] * 12})
    res = await D.converge(PROGRAM, evaluate=ev, critique=cr, direction=DIR, max_rounds=8, replan=rp, max_replans=1)
    assert calls["replan"] == 1
    assert res["stop_reason"] == "plateau"
    assert any(h.get("action") == "replan" for h in res["history"])
    assert res["best"]["round"] == 0


async def test_director_can_call_it_done_only_when_hard_checks_pass():
    ev, cr, rp, calls = fake_tools({"evals": [(True, 50)], "crits": [(6.5, "done")]})
    res = await D.converge(PROGRAM, evaluate=ev, critique=cr, direction=DIR, max_rounds=4, replan=rp)
    assert res["stop_reason"] == "director_done" and len(res["history"]) == 1
    ev, cr, rp, calls = fake_tools({"evals": [(False, 50), (True, 50)], "crits": [(6.5, "done"), (6.5, "done")]})
    res = await D.converge(PROGRAM, evaluate=ev, critique=cr, direction=DIR, max_rounds=4, replan=rp)
    assert res["history"][1]["accepted"] and res["stop_reason"] == "director_done"


def test_layout_puts_climax_at_apex_and_keeps_journey_order():
    sizes = {"a": [30, 30, 10], "b": [40, 40, 20], "c": [30, 30, 10]}
    lay = D.layout(["a", "b", "c"], sizes, climax="b", gap=8)
    xs = [x["at"][0] for x in lay]
    assert xs == sorted(xs)
    assert max(lay, key=lambda x: x["at"][1])["id"] == "b"
    assert xs[1] - xs[0] >= (30 + 40) / 2 + 8 - 1e-6
    st, log = D.apply_layout_ops({"order": ["a", "b", "c"]}, [{"op": "swap", "a": "a", "b": "c"},
                                                            {"op": "spacing", "factor": 9},
                                                            {"op": "rotate", "scene": "c", "deg": 20}], ["a", "b", "c"])
    assert st["order"] == ["a", "b", "c"] and st["spacing"] == 1.6 and st["rotations"] == {"c": 20.0}
    assert "相邻" in log[0]
    pts = D.transform_points([[1, 0, 2]], [10, 5, 0], 90)
    assert pts == [[10.0, 6.0, 2.0]]


def test_world_checks_scale_palette_and_rhythm():
    b = D.normalize_bible(bible())
    progs = {s["id"]: D.lock_program({"items": [{"type": "stack", "at": [0, 0], "tiers": [{"r": .2, "top": 4 + (s["role"] == "climax") * 4}]}]},
                                     b, s, "citadel") for s in b["scenes"]}
    checks = {c["name"]: c for c in D.world_checks(progs, b)}
    assert all(c["passed"] for c in checks.values())
    progs["s0"]["level_h"] = 3.0
    progs["s0"]["palette"] = "sea_stack"
    progs["s0"]["items"][0]["tiers"][0]["top"] = 12
    checks = {c["name"]: c for c in D.world_checks(progs, b)}
    assert not checks["尺度一致（格网 / 层高）"]["passed"] and not checks["色彩角色统一"]["passed"]
    assert not checks["节奏：高潮场景最高"]["passed"] and not checks["节奏：高潮场景最高"]["required"]


def atlas_state(director=7.5, world=7.0):
    b = D.normalize_bible(bible(2))
    ok = [{"name": "无悬空", "required": True, "passed": True}]
    return {"atlas": {"bible": b, "direction": DIR,
                      "targets": {s["id"]: {"director": 7.0, "buildable": True} for s in b["scenes"]},
                      "scenes": {s["id"]: {"glb": "x.glb", "checks": ok, "director": director, "metrics": {"dino": 0.4},
                                           "start_director": 4.0, "stop_reason": "converged"} for s in b["scenes"]},
                      "world": {"checks": ok, "review": {"director": world}, "director": world, "passed": world >= 6.5,
                                "world_pass": 6.5, "glb": "w.glb"},
                      "godot": {"ok": True, "checks": ok, "match": {"hero": 0.9}}}}


def test_quality_gate_for_atlas():
    assert assess({"mode": "atlas"}, atlas_state())["status"] == "passed"
    q = assess({"mode": "atlas"}, atlas_state(director=5.0))
    assert q["status"] == "blocked" and q["suggested_stage"] == "atlas_scenes"
    q = assess({"mode": "atlas"}, atlas_state(world=5.0))
    assert q["status"] == "blocked" and any(c["name"] == "世界整合导演评审" for c in q["issues"])
    st = atlas_state()
    st["atlas"]["targets"]["s0"]["director"] = 3.0
    assert assess({"mode": "atlas"}, st)["status"] == "unverified"
    st["atlas"]["targets"]["s0"]["human_confirmed"] = True
    assert assess({"mode": "atlas"}, st)["status"] == "passed"
    assert assess({"mode": "atlas"}, {})["status"] == "unverified"


@pytest.mark.parametrize("raw,ok", [({d["id"]: 5 for d in DIMS}, True), ({"silhouette": 5}, False),
                                    ({d["id"]: "5" for d in DIMS}, False)])
def test_scores_must_cover_every_dimension(raw, ok):
    assert (D.clamp_scores(raw, DIMS) is not None) is ok


# ───────────── 端到端：五个阶段 + 交付，工具全部替身（验证编排、记录与门禁，不验证画质） ─────────────

class FakeHub:
    def __init__(self, run_dir, **kw):
        self.dir = run_dir
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def call(self, name, args):
        from PIL import Image
        self.calls.append(name)
        if name == "blender_build_oskar":
            return {"ok": True, "checks": [{"name": "无悬空", "required": True, "passed": True}],
                    "walk": [[0, 0, 2], [3, 0, 2]], "paths": [[[0, 0, 2], [3, 0, 4]]], "size": [30, 28, 12]}
        if name == "blender_export_glb":
            p = self.dir / "blender" / "export" / f"{args['asset_name']}.glb"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"glTF")
            return {"ok": True, "path": str(p), "info": {"size": [30, 28, 12], "triangles": 100}}
        if name == "blender_compose_scene":
            return {"placed": [i["id"] for i in args["items"]], "errors": [], "size": [120, 60, 20]}
        if name == "blender_compose_planet":
            assert args["radius"] > 0 and all("lat" in i and "lon" in i for i in args["items"])
            assert args["links"] == [] and any(i.get("kind") == "link" for i in args["items"])   # 地势连接，不再搭陆桥
            return {"ok": True, "errors": [], "placed": [i["id"] for i in args["items"]], "radius": args["radius"],
                    "size": [100, 100, 100], "triangles": 1000}
        if name == "blender_run_python":
            assert "oskar_planet" in args["code"]
            return {"ok": True}
        if name == "blender_render":
            out = []
            for v in args["views"]:
                p = self.dir / "renders" / f"{args['label']}_{v}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (8, 8), (120, 160, 180)).save(p)
                out.append(str(p))
            return {"images": out}
        raise AssertionError(name)


@pytest.mark.parametrize("form", ["planet", "archipelago"])
async def test_atlas_stages_end_to_end_with_stubbed_tools(tmp_path, monkeypatch, form):
    from PIL import Image
    from genai_pipeline import config, vision, world_atlas
    from genai_pipeline.agent import RunControl
    from genai_pipeline.oskar import agent as OA
    from genai_pipeline.godot import oskar_scene as OG
    from genai_pipeline.pipeline import STAGES, RunContext, stage_deliver
    from genai_pipeline.store import Store

    monkeypatch.setitem(config.load()["pipeline"], "runs_dir", str(tmp_path / "runs"))
    monkeypatch.setattr(style_agents, "_root", lambda: tmp_path / "agents")
    style_agents.save_journal("oskar", "older-run", [{"scene": "旧港", "ops": {"scale": {"tried": 3, "accepted": 0}},
                                                      "note": "放大救不了平直的剪影"}])
    store = Store(tmp_path / "s.sqlite")
    opts = {"style_agent": "oskar", "style_profile": style_agents.snapshot("oskar"), "approval_gates": [],
            "concept_candidates": 2, "atlas_rounds": 3, "atlas_compose_rounds": 2, "atlas_form": form}
    rid = store.create_run("世界", "海上三站的旅程", "atlas", opts, STAGES["atlas"])
    ctx = RunContext(store, store.get_run(rid), RunControl())
    seen = {"journal_in_plan": False, "anchor_for": []}

    async def world_bible(brief, **kw):
        return D.normalize_bible(bible(3))

    async def generate_image(prompt, out, **kw):
        assert DIR["target_style_lock"] in prompt
        Image.new("RGB", (8, 8), (200, 190, 170)).save(out)
        return {"seconds": 1, "backend": "flux"}

    draws = {"n": 0}

    async def judge_targets(cands, *, scene, anchor=None, **kw):
        draws["n"] += 1
        seen["anchor_for"].append((scene["id"], bool(anchor)))
        weak = scene["id"] == "s0" and draws["n"] <= 2     # 开场第一稿不够好：Oskar 改写提示词重画
        c = [{"scores": scores(5 if weak else 7), "director": 5 if weak else 7, "buildable": True, "note": ""} for _ in cands]
        return {"candidates": c, "best": len(cands) - 1, "direction": "主峰要更孤绝", "revised_prompt": "a lonelier sea stack",
                "consistency": None, "action": "redraw" if weak else "accept"}

    async def plan(target, out, *, style_rules="", **kw):
        seen["journal_in_plan"] |= "过往世界的工作室手记" in style_rules
        out.mkdir(parents=True, exist_ok=True)
        return {"program": copy.deepcopy(PROGRAM) | {"palette": "citadel"}, "read": {}, "facts": ""}

    objective = {"v": 50.0}

    async def evaluate_program(hub, target, pg, tag, views=("hero", "hero_left")):
        objective["v"] += 0.5
        r = await hub.call("blender_render", {"views": ["hero"], "label": tag})
        return {"ok": True, "checks_ok": True, "checks": [{"name": "无悬空", "required": True, "passed": True}],
                "metrics": {"dino": 0.4, "siglip": 0.8}, "objective": objective["v"], "images": r["images"]}

    async def critique(rec, phase, failed, **kw):
        assert "工作室手记" in kw["journal"]
        v = 6 + rec["round"]
        return {"scores": scores(v), "verdict": "台面再宽一点", "action": "edit",
                "diffs": [{"what": "台面窄", "op": "scale", "item": 0, "factor": 1.1}]}

    async def world_review(imgs, targets, **kw):
        return {"scores": {d["id"]: 7 for d in D.rubric(DIR, "world_rubric")}, "director": 7.0, "verdict": "旅程成立",
                "layout_ops": [{"op": "spacing", "factor": 1.2}], "scene_notes": {"s0": "开场再低一些"}}

    async def similarity(a, b):
        return {"siglip": 0.82, "dino": 0.5, "siglip_per": [0.82] * len(b), "dino_per": [0.5] * len(b)}

    async def setup_and_verify(hub, proj, **kw):
        assert kw["glb_name"] == "oskar_atlas.glb" and len(kw["extra"]["journey"]) == 3
        assert ("planet" in kw["extra"]) == (form == "planet")
        proj.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "checks": [{"name": "运行", "required": True, "passed": True, "detail": ""}], "shots": [],
                "labels": [], "match": {"hero": 0.9}, "fps": 60, "project": str(proj)}

    monkeypatch.setattr(D, "world_bible", world_bible)
    monkeypatch.setattr(D, "judge_targets", judge_targets)
    monkeypatch.setattr(D, "critique", critique)
    monkeypatch.setattr(D, "world_review", world_review)
    link_calls = []

    async def plan_links(specs, **kw):
        link_calls.append([sp["id"] for sp in specs])
        return D.normalize_link_plan({"links": [{"id": specs[0]["id"], "kind": "ridge", "width": 14, "why": "两端都高"}]}, specs)
    monkeypatch.setattr(D, "plan_links", plan_links)
    monkeypatch.setattr(vision, "generate_image", generate_image)
    monkeypatch.setattr(vision, "objective_similarity", similarity)
    monkeypatch.setattr(OA, "plan", plan)
    monkeypatch.setattr(OA, "evaluate_program", evaluate_program)
    monkeypatch.setattr(OG, "setup_and_verify", setup_and_verify)
    monkeypatch.setattr(world_atlas, "MCPHub", lambda d, **kw: FakeHub(d))
    import genai_pipeline.pipeline as PL
    monkeypatch.setattr(PL, "MCPHub", lambda d, **kw: FakeHub(d))

    for stage in ("brief", "targets", "scenes", "compose", "godot"):
        ctx.stage = "atlas_" + stage
        await getattr(world_atlas, stage)(ctx)
    atlas = ctx.state["atlas"]
    assert atlas["prior_journal"] and seen["journal_in_plan"]
    assert seen["anchor_for"][0] == ("s1", False) and ("s0", True) in seen["anchor_for"]
    assert len(ctx.state["concepts"]["s0"]["oskar"]["attempts"]) == 2          # 改写提示词重画了一次
    assert ctx.state["concepts"]["s0"]["content"] == "a lonelier sea stack"
    assert atlas["palette"]["from"] == "s1"                                     # 世界色彩取自高潮场景
    for sid, rec in atlas["scenes"].items():
        assert rec["status"] == "done" and rec["director"] >= 7.0 and rec["stop_reason"] == "converged"
        assert rec["program"]["grid"]["cell"] == 2.0
    assert len(atlas["journal"]) == 3
    assert style_agents.recent_journals("oskar", exclude="older-run")[0]["run_id"] == rid
    world = atlas["world"]
    assert world["passed"] and len(world["history"]) == 2 and world["journey"][0]["id"] == "s0"
    assert world["form"] == form
    if form == "planet":
        assert world["planet"]["radius"] > 0 and len(world["faces"]) == 3 and len(world["planet"]["links"]) >= 2
        assert len(world["paths"]) >= 3 + 2   # 三站各自的通路 + 两段地势连接上的通路
        tl = world["planet"]["terrain_links"]
        assert len(link_calls) == 1 and len(tl) == 2 and tl[0]["kind"] == "ridge" and tl[0]["width"] == 14
        assert all(t["glb"].endswith(".glb") and "walk" not in t for t in tl)
    tools = [e["message"] for e in store.events(rid, limit=5000) if e["kind"] == "tool_call"]
    assert any(t.startswith("Oskar → FLUX.draw") for t in tools) and any("Godot" in t for t in tools)
    assert any("推翻" in t for t in tools) is False   # 本次没有停滞，不需要推倒重来
    ctx.stage = "deliver"
    await stage_deliver(ctx)
    report = (ctx.dir / "deliverables" / "REPORT.md").read_text()
    assert "逐场景趋近" in report and "世界整合" in report and ctx.state["quality"]["status"] == "passed"
