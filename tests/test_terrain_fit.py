"""实测地形 → Oskar 引擎：高度图定轮廓与层数、连续面不切台阶、裸岩按实测、台地预留、地形不自带建筑。"""
import pytest

from genai_pipeline.oskar.program import build_state
from genai_pipeline.pipeline import _terrain_fit


def _relief(n=12, top=0.9, front=0.8):
    h = []
    for i in range(n):
        row = []
        for j in range(n):
            u, v = (j + 0.5) / n * 2 - 1, (i + 0.5) / n * 2 - 1
            row.append(None if u * u + v * v > 0.7 else round(front + (top - front) * (v + 1) / 2, 3))
        h.append(row)
    return h


def test_measured_relief_gives_one_flat_mesa():
    h = _relief()
    prog = {"seed": 1, "grid": {"radius": 4, "cell": 2.0}, "level_h": 1.5, "sea_level": 0,
            "items": [{"type": "terrain", "at": [0, 0], "r": 0.85, "top": 3, "relief": {"h": h}}]}
    st = build_state(prog)
    levels = {lv for _, lv in st.tiles.values()}
    assert levels == {3}                      # 0.8→0.9 的缓坡是一整块面，不切台阶
    assert st.terrain_stats["measured_cells"] == len(st.tiles)
    assert all(k == "grass" for k, _ in st.tiles.values())   # 没测到裸岩：崖顶全是草
    assert st.terrain_stats["fallback"] == 0


def test_measured_step_and_bare_top():
    n = 12
    h = _relief(n)
    bare = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if h[i][j] is not None and j >= n // 2:
                h[i][j] = 0.4                    # 右半边低一截：实测有崖坎
                bare[i][j] = 1
    prog = {"seed": 1, "grid": {"radius": 4, "cell": 2.0}, "level_h": 1.5, "sea_level": 0,
            "items": [{"type": "terrain", "at": [0, 0], "r": 0.85, "top": 3, "relief": {"h": h, "bare": bare}}]}
    st = build_state(prog)
    levels = {lv for _, lv in st.tiles.values()}
    assert levels == {1, 3}
    kinds = {k for k, lv in st.tiles.values() if lv == 1}
    assert kinds == {"rock"}


def test_fit_from_relief_locks_grid_levels_and_strips_buildings():
    rel = {"h": _relief(24), "half_m": 8.0, "size_m": [16, 15, 5], "land_cells": 200,
           "cliff": {"facet_m": 1.5, "tilt_deg": 5.0, "roughness_deg": 7.4, "facet_dev_deg": 24.0},
           "plateaus": [{"asset": "lighthouse", "at": [0.4, 0.6], "r": 0.25, "h": 0.9}]}
    a = {"id": "island", "relief": rel}
    spec = {"assets": [a, {"id": "lighthouse", "method": "image3d"}, {"id": "sea", "method": "backdrop"}]}
    fit = _terrain_fit(spec, a)
    assert fit["grid"]["radius"] == 3 and 2 <= fit["levels"] <= 8
    assert fit["rock_detail"] < 0.35 and fit["strip"] and fit["forest"] == 0.0
    assert fit["plateaus"][0]["at"] == [0.34, 0.51]
    assert fit["facet"][0] > 0.5 and fit["facet"][1] > 2   # 块面起伏按实测偏角
    assert _terrain_fit({"assets": [{"id": "x"}]}, {"id": "x"}) is None


def test_depth_fraction_is_perspective_correct():
    np = pytest.importorskip("numpy")
    from genai_pipeline.depth_layout import depth_fraction
    cam = {"fy_n": 0.913, "cy_n": 0.5, "pitch_down_deg": 3.0}
    f = depth_fraction([0.5977, 0.55, 0.5078], [0.5078, 0.5977], cam)
    assert f[0] == pytest.approx(0) and f[-1] == pytest.approx(1)
    assert f[1] < 0.5          # 行号的中点离前沿更近（远处一行代表的距离更长）


def test_relief_keeps_seen_cells_and_rounds_the_hidden_back():
    np = pytest.importorskip("numpy")
    from genai_pipeline.depth_layout import relief
    rng = np.random.default_rng(0)
    # 只看得见前半个顶面（后半被物件挡住）：行号在前沿到中间
    n = 600
    x = rng.uniform(-7, 7, n)
    rows = rng.uniform(0.56, 0.5977, n)
    pts = np.c_[x, np.full(n, 20.0), np.full(n, 4.5)]
    cliff = np.c_[rng.uniform(-7, 7, 200), np.full(200, 19.0), rng.uniform(0, 4.5, 200)]   # 崖面（不朝上）
    shape = {"points": np.r_[pts, cliff].tolist(), "up": [1] * n + [0] * 200,
             "rows": rows.tolist() + [0.7] * 200}
    rel = relief(shape, [14, 14, 5], 0.0, [0.5078, 0.5977], cam={"fy_n": 0.913, "cy_n": 0.5, "pitch_down_deg": 3.0})
    h = rel["h"]
    assert rel["back_inferred_cells"] > 0
    assert h[0][12] is not None and h[23][12] is not None      # 前沿、后沿（补出来的）都有陆地
    assert h[23][0] is None and h[23][23] is None              # 看不见的后角按椭圆收圆
    assert all(v is None or 0.8 <= v <= 1.0 for row in h for v in row)


def test_vegetation_none_builds_no_props():
    from genai_pipeline.oskar import build as B
    from genai_pipeline.oskar.program import build_state
    h = _relief()
    prog = {"seed": 1, "grid": {"radius": 4, "cell": 2.0}, "level_h": 1.5, "sea_level": 0, "vegetation": "none",
            "rock_detail": 0.0, "items": [{"type": "terrain", "at": [0, 0], "r": 0.85, "top": 3, "relief": {"h": h}}]}
    out = B.build_mesh(build_state(prog), seed=1)
    parts = out["parts"]
    roles = set(parts) if isinstance(parts, dict) else {p.get("role") for p in parts}
    assert not roles & {"shrub", "canopy", "trunk", "cypress"}, roles


def test_facet_relief_tilts_cliff_faces():
    from genai_pipeline.oskar import build as B
    from genai_pipeline.oskar.program import build_state
    import math

    def spread(facet):
        prog = {"seed": 3, "grid": {"radius": 4, "cell": 2.0}, "level_h": 1.5, "sea_level": 0, "vegetation": "none",
                "rock_detail": 0.0, "items": [{"type": "terrain", "at": [0, 0], "r": 0.85, "top": 3, "relief": {"h": _relief()}}]}
        if facet:
            prog["facet"] = facet
        st = build_state(prog)
        d = B.make_deform(st, 3)
        pts = [(math.cos(a) * 8, math.sin(a) * 8, 2.0) for a in [i * 0.3 for i in range(20)]]
        moved = [d(p, "rock", None) for p in pts]
        return max(math.dist(p[:2], q[:2]) for p, q in zip(pts, moved))
    assert spread([0.9, 3.0]) > spread(None) + 0.2


def test_color_check_reports_exposure_stops(tmp_path):
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    import sys
    sys.path.insert(0, "src/genai_pipeline")
    import color_palette as CP
    a = np.full((64, 64, 3), 180, np.uint8)
    Image.fromarray(a).save(tmp_path / "t.png")
    Image.fromarray((a * 0.7).astype(np.uint8)).save(tmp_path / "r.png")
    m = np.zeros((64, 64), np.uint8)
    m[10:50, 10:50] = 255
    Image.fromarray(m).save(tmp_path / "m.png")
    res = CP.check(str(tmp_path / "t.png"), str(tmp_path / "r.png"), {"asset:x": str(tmp_path / "m.png")})
    assert 1.0 < res["exposure_stops"] < 1.3     # 渲染暗了约 1.1 档（线性亮度）


def test_sea_plate_off_keeps_foam_only():
    from genai_pipeline.oskar import build as B
    from genai_pipeline.oskar.program import build_state
    prog = {"seed": 1, "grid": {"radius": 4, "cell": 2.0}, "level_h": 1.5, "sea_level": 0, "vegetation": "none",
            "sea_plate": False, "items": [{"type": "terrain", "at": [0, 0], "r": 0.85, "top": 3, "relief": {"h": _relief()}}]}
    parts = B.build_mesh(build_state(prog), seed=1)["parts"]
    assert "foam" in parts and not parts.get("sea") and not parts.get("sea_deep")


def test_measured_top_stays_grass_in_mediterranean_style():
    """实测地形的顶面草 / 岩只听实测：citadel 调色板（地中海风格）的随机裸岩斑不能把整片草顶变成岩顶。"""
    from genai_pipeline.oskar import build as OB, program as OP
    n = 8
    h = [[0.9 if 1 <= i <= 6 and 1 <= j <= 6 else None for j in range(n)] for i in range(n)]
    bare = [[False] * n for _ in range(n)]
    pg = {"palette_base": "citadel", "vegetation": "none", "seed": 84, "grid": {"radius": 3, "cell": 2.0},
          "level_h": 1.8, "sea_level": 0, "sea_plate": False,
          "items": [{"type": "terrain", "at": [0, 0], "r": 0.85, "top": 3, "forest": 0.0,
                     "relief": {"h": h, "bare": bare}}]}
    st = OP.build_state(pg)
    assert st.style == "mediterranean" and st.measured_groups
    parts = OB.build_mesh(st, seed=84)["parts"]
    grass = sum(1 for r in parts if r.startswith("grass"))
    assert grass >= 1 and "rock_top" not in parts
