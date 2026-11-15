"""六边形对偶格 + 瓦片 WFC 地形（Oskar 星球项目做法）的单元测试：纯 Python，不需要 Blender / LLM。"""
import json
import math
from pathlib import Path

from genai_pipeline.oskar.build import build_mesh
from genai_pipeline.oskar.grid import grid_stats, make_grid
from genai_pipeline.oskar.program import build_state, check_state, grid_kind, validate_program
from genai_pipeline.oskar.prompts import apply_edits, examples_text
from genai_pipeline.oskar.tiles import TerrainWFC, Tile, compatible, make_tiles

ROOT = Path(__file__).resolve().parents[1]


def test_hex_grid_is_triangles_with_hexagonal_dual_cells():
    g1, g2 = make_grid(radius=6, seed=3, kind="hex"), make_grid(radius=6, seed=3, kind="hex")
    st = grid_stats(g1)
    assert g1.kind == "hex" and all(len(f) == 3 for f in g1.quads)
    assert st["inverted"] == 0 and st["angles_bad"] == 0
    sides = st["cell_sides"]
    assert set(sides) <= {5, 6, 7} and sides[6] / sum(sides.values()) >= 0.8   # 六边形为主，零星 5 / 7 边形
    assert sides.get(5, 0) > 0 and sides.get(5, 0) == sides.get(7, 0)          # 每次边翻转产生一对 5 / 7
    assert g1.verts == g2.verts and g1.quads == g2.quads
    assert make_grid(radius=6, seed=4, kind="hex").verts != g1.verts
    # 场景尺寸与旧四边形网格一致（radius × 2 × cell）
    span = max(abs(p[0]) for p in g1.verts)
    assert abs(span - max(abs(p[0]) for p in make_grid(radius=6, seed=3).verts)) < 2.5
    # 角块总是 4 点；内部顶点的对偶格边数 = 邻居数
    v = next(i for i in range(len(g1.verts)) if not g1.boundary[i])
    assert all(len(g1.quarter(q, g1.quads[q].index(v))) == 4 for q in g1.vert_quads[v])
    assert len(g1.dual_cell(v)) == 2 * len(g1.neighbors[v])


def test_programs_default_to_hex_and_quad_stays_available():
    assert grid_kind({}) == "hex" and grid_kind({"grid": {"kind": "quad"}}) == "quad"
    prog = json.loads((ROOT / "projects/oskar-trials/programs/citadel.ref.json").read_text())
    for kind in ("hex", "quad"):
        p = dict(prog, grid=dict(prog["grid"], kind=kind))
        st = build_state(p)
        assert st.grid.kind == kind
        assert all(c["passed"] for c in check_state(st)), kind
        assert build_mesh(st, seed=1)["segments"] > 0


def test_tile_rules():
    sea, cliff = 1, 3
    assert compatible(Tile("grass", 3), Tile("forest", 4), sea, cliff)
    assert not compatible(Tile("grass", 3), Tile("grass", 5), sea, cliff)          # 草坡不陡落
    assert compatible(Tile("rock", 5), Tile("grass", 3), sea, cliff)               # 裸岩崖可以
    assert not compatible(Tile("rock", 6), Tile("grass", 2), sea, cliff)           # 但不超过 cliff_max
    assert compatible(Tile("sea", 1), Tile("grass", 2), sea, cliff)
    assert not compatible(Tile("sea", 1), Tile("grass", 3), sea, cliff)            # 海边只接最低一级
    assert compatible(Tile("sea", 1), Tile("rock", 4), sea, cliff)                 # 或者海崖
    kinds = {t.kind for t in make_tiles(1, 6)}
    assert kinds == {"sea", "grass", "forest", "rock"}


def _terrain_prog(seed=5):
    return json.loads((ROOT / "projects/oskar-trials/programs/hex-island.ref.json").read_text()) | {"seed": seed}


def test_terrain_wfc_obeys_rules_reserves_plateau_and_has_no_pits():
    prog = _terrain_prog()
    assert validate_program(prog) is None
    st = build_state(prog)
    g = st.grid
    ts = st.terrain_stats
    assert ts["fallback"] == 0 and ts["reserved"] > 0
    assert {"grass", "forest", "sea"} <= set(ts["tiles"])
    tile = {v: Tile(*st.tiles[v]) if v in st.tiles else Tile("sea", st.sea_level) for v in range(len(g.verts))}
    for v in range(len(g.verts)):
        for w in g.neighbors[v]:
            assert compatible(tile[v], tile[w], st.sea_level, 3), (tile[v], tile[w])
    assert all(tile[v].kind == "sea" for v in range(len(g.verts)) if g.boundary[v])
    # 侵蚀约束：草 / 林格不会四周全比自己高
    for v, t in tile.items():
        if t.kind in ("grass", "forest"):
            assert min(tile[w].level for w in g.neighbors[v]) <= t.level
    # 主峰达到 top；台地整片同一层（大模块先要地）
    assert max(t.level for t in tile.values()) == 7
    S = g.radius * g.cell * 2 * 0.85
    px, py = -0.1 * S, -0.3 * S
    plateau = [v for v in st.tiles if math.hypot(g.verts[v][0] - px, g.verts[v][1] - py) <= 0.14 * S]
    assert plateau and {st.tiles[v][1] for v in plateau} == {3}
    # 林地成片：多数林地格至少有一个林地邻格
    forest = [v for v, t in tile.items() if t.kind == "forest"]
    assert sum(any(tile[w].kind == "forest" for w in g.neighbors[v]) for v in forest) >= 0.7 * len(forest)
    assert len(st.trees) == len(forest)
    assert all(c["passed"] for c in check_state(st))
    m = build_mesh(st, seed=1)
    assert m["props"]["trees"] >= len(forest) and "rock_top" in m["parts"] and "canopy" in m["parts"]


def test_terrain_is_deterministic_and_seed_sensitive():
    a, b, c = build_state(_terrain_prog(5)), build_state(_terrain_prog(5)), build_state(_terrain_prog(6))
    assert a.tiles == b.tiles and a.tiles != c.tiles


def test_terrain_validation_and_edits():
    bad = {"items": [{"type": "building", "kind": "house", "at": [0, 0], "r": 0.1, "top": 2},
                     {"type": "terrain", "top": 5}]}
    assert "第一个" in validate_program(bad)
    assert "top" in validate_program({"items": [{"type": "terrain"}]})
    prog = _terrain_prog()
    moved, log = apply_edits(prog, [{"op": "move", "item": 0, "dx": 0.1, "dy": 0}])
    assert moved["items"][0]["peaks"][0]["at"][0] == round(prog["items"][0]["peaks"][0]["at"][0] + 0.1, 3)
    assert '"terrain"' in examples_text()


def test_wfc_restarts_instead_of_breaking_rules():
    # 只有两格、两格都被固定成不相容的高度：求解器必须报告兜底，而不是悄悄违反规则
    W = TerrainWFC([0, 1], [[1], [0]], sea_level=1, top=6)
    fixed = {0: W.index[Tile("grass", 6)], 1: W.index[Tile("grass", 2)]}
    W.solve(fixed, {}, seed=1, max_restarts=2)
    assert W.stats["fallback"] == 1 and W.stats["contradictions"] >= 1
