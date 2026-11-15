"""Oskar 式引擎（纯 Python 部分）单元测试：不需要 Blender / LLM。"""
import json
from pathlib import Path

from genai_pipeline.oskar.build import ALLOWED, OPEN, build_mesh
from genai_pipeline.oskar.grid import grid_stats, make_grid
from genai_pipeline.oskar.program import build_state, check_state, validate_program
from genai_pipeline.oskar.prompts import apply_edits
from genai_pipeline.oskar.wfc import WFC

ROOT = Path(__file__).resolve().parents[1]


def test_grid_all_quads_valid_and_deterministic():
    g1, g2 = make_grid(radius=5, seed=3), make_grid(radius=5, seed=3)
    st = grid_stats(g1)
    assert st["inverted"] == 0 and st["angles_bad"] == 0
    assert all(len(q) == 4 for q in g1.quads)
    assert g1.verts == g2.verts and g1.quads == g2.quads
    assert make_grid(radius=5, seed=4).verts != g1.verts


def test_wfc_respects_constraints():
    doms = {i: {"a", "b"} for i in range(6)}
    rels = [(i, i + 1, "h") for i in range(5)]
    allowed = {"h": {("a", "b"), ("b", "a")}}
    out = WFC(doms, {"a": 1, "b": 1}, allowed, rels, fallback="a").solve(seed=1)
    assert all(out[i] != out[i + 1] for i in range(5))


def test_facade_rules_in_allowed_tables():
    assert ("window", "blank") not in ALLOWED["v"]          # 窗列上下对齐
    assert ("blank", "door") not in ALLOWED["v"]            # 门不在上层
    assert all(not (a in OPEN and b in OPEN) for a, b in ALLOWED["c"])  # 墙角不同时开口


def test_ground_fill_and_checks():
    prog = {"seed": 1, "grid": {"radius": 5}, "items": [
        {"type": "mass", "kind": "plinth", "at": [0, 0], "size": [0.6, 0.6], "top": 2},
        {"type": "building", "kind": "tower", "at": [0.5, 0.5], "r": 0.12, "bottom": 3, "top": 6}]}
    assert validate_program(prog) is None
    st = build_state(prog)
    assert any("自动补石台基" in n for n in st.notes)
    assert all(c["passed"] for c in check_state(st) if c["name"].startswith("无悬空"))


def test_path_blocked_is_reported():
    prog = {"seed": 1, "grid": {"radius": 5}, "items": [
        {"type": "path", "points": [[-0.4, 0], [0.4, 0]], "level": 0, "clear": 2},
        {"type": "mass", "kind": "plinth", "at": [0, 0], "size": [0.3, 0.3], "top": 2}]}
    checks = check_state(build_state(prog))
    path = [c for c in checks if c["name"].startswith("通路")][0]
    assert not path["passed"]


def test_apply_edits_clamps_and_skips():
    prog = {"items": [{"type": "stack", "at": [0, 0], "tiers": [{"r": 0.2, "top": 4}]}]}
    newp, log = apply_edits(prog, [{"op": "move", "item": 0, "dx": 5, "dy": 0},
                                   {"op": "top", "item": 0, "tier": -1, "delta": 9},
                                   {"op": "scale", "item": 7, "factor": 2}])
    assert newp["items"][0]["at"] == [0.2, 0]
    assert newp["items"][0]["tiers"][0]["top"] == 8
    assert any("不存在" in x for x in log)
    assert prog["items"][0]["at"] == [0, 0]   # 原程序不变


def test_reference_programs_build_deterministically():
    for name in ("sea-stacks.ref.json", "citadel.ref.json"):
        prog = json.loads((ROOT / "projects/oskar-trials/programs" / name).read_text())
        st = build_state(prog)
        assert all(c["passed"] for c in check_state(st) if c.get("required")), name
        a = build_mesh(st, seed=prog["seed"])
        b = build_mesh(build_state(prog), seed=prog["seed"])
        assert a["tiles"] == b["tiles"] and a["wfc"]["fallbacks"] == 0
        if name.startswith("citadel"):
            assert a["tiles"].get("door", 0) >= 1 and a["tiles"].get("slit", 0) >= 1


def test_look_shared_by_blender_and_godot():
    from genai_pipeline.blender import look
    from genai_pipeline.godot.oskar_scene import scene_config
    # 与 Blender mathutils 实测一致：Euler((32,6,38)°) · (0,0,-1)
    d = look.sun_direction((32, 6, 38))
    assert [round(x, 3) for x in d] == [-0.396, 0.363, -0.843]
    assert look.to_godot((1, 2, 3)) == [1, 3, -2]          # Blender Z 向上 → Godot Y 向上
    cfg = scene_config()
    assert abs(cfg["fov"] - 39.598) < 0.01
    assert cfg["views"]["hero"][1] > 0 and cfg["views"]["hero"][2] > 0   # 机位在上方、镜头一侧（Godot +Z）
    day = cfg["lighting"]["day"]
    assert len(day["lights"]) == 3 and abs(day["lights"][0]["energy"] - 3.3 / 3.14159) < 0.01
    assert cfg["ignore_bounds"] == ["oskar_sea"]


def test_mediterranean_modules_and_waterfall():
    prog = json.loads((ROOT / "projects/oskar-trials/programs/citadel.ref.json").read_text())
    st = build_state(prog)
    assert st.style == "mediterranean" and st.waterfalls
    # 主楼 + dome → 平顶 + 中央鼓座（塔楼组，roof dome）
    assert "dome" in {st.group_roof[g] for g, k in st.group_kind.items() if k == "tower"}
    out = build_mesh(st, seed=prog["seed"])
    assert out["tiles"].get("loggia", 0) > 0 and out["tiles"].get("window", 0) == 0   # 顶层拱廊、窗全为拱窗
    assert out["props"]["waterfall_drops"] >= 2 and out["props"]["cypress"] > 0
    assert "water" in out["parts"]
    assert ("crack", "strata") not in ALLOWED["v"] and ("crack", "crack") in ALLOWED["v"]   # 裂隙通高


def test_bridge_has_piers_and_arches():
    prog = {"seed": 2, "grid": {"radius": 6}, "sea_level": 1, "palette": "sea_stack", "items": [
        {"type": "bridge", "points": [[-0.8, -0.3], [0.8, -0.3]], "level": 4}]}
    assert validate_program(prog) is None
    st = build_state(prog)
    piers = {v for (v, h) in st.cells if h == 0}
    assert piers and st.arch_span and all(c["passed"] for c in check_state(st) if c["required"])
    out = build_mesh(st, seed=2)
    assert "sea_deep" in out["parts"] and out["props"].get("cypress", 0) == 0   # 桥面不种树


def test_stair_ramps_and_walk_data_for_godot():
    from genai_pipeline.godot.oskar_scene import MAIN_GD_GAME, scene_config
    prog = json.loads((ROOT / "projects/oskar-trials/programs/citadel.ref.json").read_text())
    out = build_mesh(build_state(prog), seed=prog["seed"])
    assert out["props"]["ramps"] >= 1                       # 通路升层处有坡道
    assert len(out["walk"]) > 50 and out["paths"] and out["paths"][0]
    cfg = scene_config("oskar_world.glb", walk=out["walk"], paths=out["paths"], game=True)
    assert cfg["orbit"] is False and cfg["showcase"] is False and cfg["tour"]
    assert "oskar_props" in cfg["no_collision"]              # 灌木 / 树不做碰撞，免得卡住玩家
    p = out["paths"][0][0]
    s = cfg["spawn"]
    assert s in cfg["walk"] and abs(s[1] - p[2]) <= 2.01    # 出生点 = 通路起点附近的平地点（Godot 坐标），不在坡道上
    assert MAIN_GD_GAME.startswith('extends "res://addons/pipeline/oskar_diorama.gd"') and "super()" in MAIN_GD_GAME


def test_auto_palette_derives_roles():
    from genai_pipeline.oskar.palette import auto_palette, resolve
    pal = auto_palette({"rock": "#283D5F", "sea": "#336EB7", "crystal": "#97C3E6"}, "sea_stack")
    assert pal["rock"] == "#283D5F" and pal["sea"] == "#336EB7"
    assert pal["rock_dark"] != pal["rock"] and pal["sea_deep"] != pal["sea"] and "crystal_alt" in pal
    assert resolve(pal, "sea_stack")["rock"] == "#283D5F" and resolve("citadel")["dome"]
