"""2026-10-06 山形地势专题落地：收分、轮廓软化、台缘、色斑、波纹、色域、云与夜光配置（纯 Python，不需要 Blender）。"""
import json
import math
from pathlib import Path

from genai_pipeline.oskar import palette as PAL
from genai_pipeline.oskar.build import OutlineField, TerrainField, build_mesh, make_deform
from genai_pipeline.oskar.program import build_state, check_state

ROOT = Path(__file__).resolve().parents[1]
STACK = {"seed": 3, "grid": {"radius": 6}, "sea_level": 1, "scene_type": "sea_stacks",
         "items": [{"type": "stack", "at": [0, 0], "tiers": [{"r": 0.42, "top": 4}, {"r": 0.28, "top": 9}]}]}


def _radial(st, deform, z):
    """一圈探针点在高度 z 处被形变后的平均半径（相对岩柱中心）。"""
    out = []
    for k in range(24):
        a = 2 * math.pi * k / 24
        p = (6.0 * math.cos(a), 6.0 * math.sin(a), z)
        q = deform(p, "rock", next(c.group for c in st.cells.values() if c.kind == "rock"))
        out.append(math.hypot(q[0], q[1]) - math.hypot(p[0], p[1]))
    return sum(out) / len(out)


def test_batter_spreads_low_rock_more_than_high_rock():
    st = build_state({**STACK, "soften": 0})
    field = TerrainField(st)
    assert field.ok and field.sample(0, 0) > field.sample(20, 0)
    on = make_deform(st, 1)
    off = make_deform(build_state({**STACK, "soften": 0, "batter": 0}), 1)
    low, high = st.z(2), st.z(8)
    spread_low = _radial(st, on, low) - _radial(st, off, low)
    spread_high = _radial(st, on, high) - _radial(st, off, high)
    assert spread_low > 0.3 and spread_low > spread_high + 0.2   # 高处收、低处放


def test_soften_defaults_follow_scene_type_and_never_moves_far():
    assert build_state(STACK).soften == 1.2
    town = build_state({**STACK, "scene_type": "town"})
    assert town.soften == 0.8 and build_state({**STACK, "scene_type": "town", "soften": 0}).soften == 0
    st = build_state(STACK)
    f = OutlineField(st)
    for v in range(0, len(st.grid.verts), 7):
        x, y = st.grid.verts[v]
        dx, dy = f.pull(x, y, st.z(3), 0.7)
        assert math.hypot(dx, dy) <= 0.7 + 1e-9


def test_soften_displacement_is_smooth_between_neighbours():
    """位移场平滑：位移的变化率小于 1（相距 0.5 米的两点位移差 < 0.5 米），映射保持单射，相邻顶点不会交叉折叠。"""
    st = build_state(STACK)
    f = OutlineField(st)
    z = st.z(3)
    worst = 0.0
    for i in range(-30, 30):
        for j in range(-30, 30, 3):
            a = f.pull(i * 0.5, j * 0.5, z, 1.2)
            b = f.pull(i * 0.5 + 0.5, j * 0.5, z, 1.2)
            worst = max(worst, math.hypot(a[0] - b[0], a[1] - b[1]))
    assert worst < 0.48


def test_deform_is_a_pure_function_of_position():
    st = build_state(STACK)
    d1, d2 = make_deform(st, 5), make_deform(build_state(STACK), 5)
    p = (3.3, -1.2, st.z(4))
    assert d1(p, "rock", 0) == d2(p, "rock", 0)   # 共享顶点两次计算结果一致 → 不会裂缝
    assert d1(p, "plinth", 0) == p


def test_ground_edges_ripples_and_roof_space_in_built_mesh():
    prog = json.loads((ROOT / "projects/oskar-trials/programs/citadel.ref.json").read_text())
    st = build_state(prog)
    assert all(c["passed"] for c in check_state(st) if c.get("required"))
    out = build_mesh(st, seed=prog["seed"])
    parts = out["parts"]
    assert out["props"]["ripple_segments"] > 50 and "ripple" in parts
    assert {"grass", "grass_dark", "grass_alt"} <= set(parts)
    no_sea = build_mesh(build_state({**STACK, "sea_level": None, "scene_type": "other"}), seed=1)
    assert "ripple" not in no_sea["parts"]


def test_palette_color_space_and_derived_roles():
    assert PAL.color_space(["#000000", "#FFFFFF"], [1, 1]) == "#808080"
    assert PAL.color_space(["#FF0000", "#00FF00", "#0000FF"], [1, 0, 0]) == "#FF0000"
    pal = PAL.resolve("citadel")
    for role in ("grass_alt", "grass_dark", "roof_b", "roof_c", "ripple"):
        assert role in pal
    assert PAL.resolve({"roof": "#C0643F"}, "citadel")["roof_b"] != PAL.resolve("citadel")["roof_b"]


def test_godot_config_has_flowing_clouds_and_night():
    from genai_pipeline.godot import oskar_scene as OG
    cfg = OG.scene_config()
    assert set(cfg["lighting"]) == {"day", "dusk", "night"}
    assert cfg["clouds"]["count"] > 0 and cfg["clouds"]["in_showcase"] is False
    assert cfg["night"]["patrol"] is True
    gd = (ROOT / "templates/godot_project/addons/pipeline/oskar_diorama.gd").read_text()
    for fn in ("func _move_clouds", "func add_lantern", "func add_patrol_light", "intersect_ray"):
        assert fn in gd


def test_new_town_reference_program_passes_hard_checks():
    prog = json.loads((ROOT / "projects/oskar-trials/programs/new-town.ref.json").read_text())
    st = build_state(prog)
    assert all(c["passed"] for c in check_state(st) if c.get("required"))
    out = build_mesh(st, seed=prog["seed"])
    assert {"roof", "roof_b", "roof_c"} & set(out["parts"]) and out["props"]["ripple_segments"] > 0
