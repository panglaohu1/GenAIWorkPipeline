"""物件改由 Oskar 引擎构件建模：几何确定、尺寸 / 颜色 / 色带只认实测，LLM 只做结构选择。"""
import pytest

from genai_pipeline.oskar import props as P
from genai_pipeline.pipeline import _engine_prop_spec


def _bbox(parts):
    vs = [v for p in parts.values() for v in p["v"]]
    return [min(v[i] for v in vs) for i in range(3)], [max(v[i] for v in vs) for i in range(3)]


@pytest.mark.parametrize("spec", [
    {"type": "tower", "size_m": [3.0, 3.0, 11.0]},
    {"type": "house", "size_m": [4.0, 3.5, 5.0], "windmill": True, "blade_r": 2.5},
    {"type": "house", "size_m": [3.0, 3.0, 3.5], "roof": "hip"},
    {"type": "pier", "size_m": [4.0, 7.0, 1.0], "shape": "L"},
    {"type": "boat", "size_m": [3.0, 1.3, 0.9]},
])
def test_props_fit_measured_size(spec):
    out = P.build(spec)
    lo, hi = _bbox(out["parts"])
    w, d, h = spec["size_m"]
    assert hi[0] - lo[0] <= max(w, d) * 1.25 and hi[2] <= h * 1.05 + 0.01
    assert all(r in out["palette"] for r in out["parts"])


def test_tower_bands_color_the_body_top_down():
    bands = [{"from": 0.0, "to": 0.5, "hex": "#E25659"}, {"from": 0.5, "to": 1.0, "hex": "#F2F0EE"}]
    out = P.build({"type": "tower", "size_m": [3, 3, 10], "bands": bands, "lantern": False, "gallery": False})
    top = max(v[2] for v in out["parts"]["band0"]["v"])
    bottom = max(v[2] for v in out["parts"]["band1"]["v"])
    assert top > bottom                       # band0（自上而下第一段）在上面
    assert out["palette"]["band0"] == "#E25659" and out["palette"]["band1"] == "#F2F0EE"


def test_spec_uses_measured_size_and_swatches_only():
    a = {"id": "lh", "size_m": [3.2, 3.2, 11.0]}
    pm = {"base": "#E25659", "swatches": [{"hex": "#E25659", "share": 0.5}, {"hex": "#2F3036", "share": 0.2}],
          "bands": [{"from": 0, "to": 1, "hex": "#E25659"}]}
    choice = {"type": "tower", "tower": {"cap": "cone", "taper": 3.0, "colors": {"trim": 1, "roof": 9}}}
    spec = _engine_prop_spec(choice, a, pm)
    assert spec["size_m"] == [3.2, 3.2, 11.0] and spec["taper"] == 1.0 and spec["cap"] == "cone"
    assert spec["colors"] == {"trim": "#2F3036"}           # 越界的色块编号丢掉，不编颜色
    assert spec["bands"] == pm["bands"]
    assert _engine_prop_spec({"type": "none"}, a, pm) is None


def test_rep_conflicts_flags_two_assets_on_one_instance(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from genai_pipeline.pipeline import _rep_conflicts
    a = Image.new("L", (100, 100), 0)
    a.paste(255, (40, 10, 70, 90))
    a.save(tmp_path / "lighthouse.png")
    a.save(tmp_path / "windmill.png")          # 同一块像素被两个资产认领
    c = Image.new("L", (100, 100), 0)
    c.paste(255, (0, 0, 20, 20))
    c.save(tmp_path / "boat.png")
    assets = [{"id": "lh"}, {"id": "wm"}, {"id": "boat"}]
    pairs = _rep_conflicts({"lh": str(tmp_path / "lighthouse.png"), "wm": str(tmp_path / "windmill.png"),
                            "boat": str(tmp_path / "boat.png")}, assets)
    assert [(x["id"], y["id"]) for x, y in pairs] == [("lh", "wm")]


def test_engine_choice_requires_params_for_the_chosen_type():
    from genai_pipeline.pipeline import _engine_choice_error
    assert _engine_choice_error({"type": "house", "why": "风车屋"})          # 只给 type：要求补参数
    assert _engine_choice_error({"type": "house", "house": {"windmill": True}})   # 没有 colors
    assert _engine_choice_error({"type": "house", "house": {"windmill": True, "colors": {"roof": 1}}}) is None
    assert _engine_choice_error({"type": "none"}) is None


def test_orbit_camera_adds_pitch_keeps_distance_and_heading():
    import math
    from genai_pipeline.pipeline import _orbit_camera
    cam = {"eye": [-0.331, -27.485, 7.481], "target": [-0.331, -0.036, 6.038], "fov_deg": 57.48, "aspect": 1.0}
    def geo(c):
        v = [e - t for e, t in zip(c["eye"], c["target"])]
        return math.dist(c["eye"], c["target"]), math.degrees(math.atan2(v[2], math.hypot(v[0], v[1]))), math.atan2(v[1], v[0])
    d0, e0, h0 = geo(cam)
    c2 = _orbit_camera(cam, 20)
    d1, e1, h1 = geo(c2)
    assert abs(d1 - d0) < 0.01 and abs(e1 - e0 - 20) < 0.05 and abs(h1 - h0) < 1e-3
    assert c2["fov_deg"] == cam["fov_deg"] and c2["target"] == cam["target"]
    assert _orbit_camera(cam, 0)["eye"] == [round(v, 3) for v in cam["eye"]]


def test_color_fix_step_corrects_relative_color_and_leaves_global_to_exposure():
    from genai_pipeline.pipeline import _color_fix_step, _srgb_to_lin
    roots = {"lh_1": "lighthouse", "rock_1": "island", "sea_1": "sea"}
    mats = [
        # 整体都偏暗一半（交给曝光）；灯塔红额外偏灰：红通道还差、绿蓝偏多
        {"root": "rock_1", "material": "osk_rock", "px": 5000, "authored": [0.87, 0.61, 0.5], "rendered": [0.64, 0.45, 0.37]},
        {"root": "lh_1", "material": "osk_red", "px": 800, "authored": [0.886, 0.337, 0.349], "rendered": [0.6, 0.3, 0.31]},
        {"root": "sea_1", "material": "strip_0", "px": 90000, "authored": [0.16, 0.41, 0.56], "rendered": [0.3, 0.5, 0.6]},
        {"root": "lh_1", "material": "tiny", "px": 10, "authored": [1, 1, 1], "rendered": [0.1, 0.1, 0.1]},
    ]
    new, info = _color_fix_step(mats, roots, {}, backdrops={"sea"}, damping=1.0)
    assert "tiny" not in new.get("lighthouse", {})          # 像素太少不调
    rock = new["island"]["osk_rock"]
    assert all(abs(f - 1.0) < 0.05 for f in rock)           # 岩石只是整体偏暗：相对色不动
    red = new["lighthouse"]["osk_red"]
    assert red[0] > 1.2 and red[0] > red[1]                 # 红通道相对要加
    assert new["sea"]["strip_0"][0] < 0.8                    # 背景照样改，但不参与整体明暗
    # 累乘：在已有系数上继续
    again, _ = _color_fix_step(mats, roots, new, backdrops={"sea"}, damping=1.0)
    assert again["lighthouse"]["osk_red"][0] > red[0]
    assert abs(_srgb_to_lin(0.5) - 0.214) < 0.001


def test_color_fix_step_skips_outline_material():
    from genai_pipeline.pipeline import _color_fix_step
    mats = [{"root": "a_1", "material": "osk_outline", "px": 900, "authored": [0.26, 0.26, 0.27], "rendered": [0.02, 0.02, 0.02]},
            {"root": "a_1", "material": "osk_wall", "px": 900, "authored": [0.8, 0.7, 0.6], "rendered": [0.6, 0.5, 0.45]}]
    new, _ = _color_fix_step(mats, {"a_1": "a"}, {})
    assert "osk_outline" not in new["a"] and "osk_wall" in new["a"]


def test_regions_worse_maps_regions_to_assets():
    from genai_pipeline.pipeline import _regions_worse
    before = {"asset:boat": {"delta_e": 10}, "terrain_top": {"delta_e": 12}, "water": {"delta_e": 20}, "asset:dock": {"delta_e": 9}}
    after = {"asset:boat": {"delta_e": 15}, "terrain_top": {"delta_e": 31}, "water": {"delta_e": 16}, "asset:dock": {"delta_e": 10}}
    out = _regions_worse(before, after, {"terrain": {"island"}, "water": {"sea"}, "sky": {"sea"}})
    assert out == {"boat", "island"}


def test_color_fix_step_limits_each_step():
    from genai_pipeline.pipeline import _color_fix_step
    mats = [{"root": "a_1", "material": "m", "px": 900, "authored": [0.9, 0.9, 0.9], "rendered": [0.2, 0.2, 0.2]},
            {"root": "b_1", "material": "n", "px": 900, "authored": [0.5, 0.5, 0.5], "rendered": [0.5, 0.5, 0.5]}]
    new, _ = _color_fix_step(mats, {"a_1": "a", "b_1": "b"}, {}, damping=1.0)
    assert max(new["a"]["m"]) <= 2.0


def test_color_check_uses_own_pixels_and_drops_water_from_box(tmp_path):
    import numpy as np
    from PIL import Image
    from genai_pipeline import color_palette as CP
    H = W = 64
    tgt = np.zeros((H, W, 3), np.uint8); tgt[:] = (55, 116, 155)          # 水
    tgt[40:50, 10:20] = (225, 224, 223)                                    # 白船（目标）
    ren = np.zeros((H, W, 3), np.uint8); ren[:] = (55, 116, 155)
    ren[44:54, 14:24] = (225, 224, 223)                                    # 渲染里船偏了 4 像素
    Image.fromarray(tgt).save(tmp_path / "t.png"); Image.fromarray(ren).save(tmp_path / "r.png")
    box = np.zeros((H, W), np.uint8); box[36:56, 6:28] = 255                # 框选区域：带进一大片水
    water = np.zeros((H, W), np.uint8); water[:30] = 255
    Image.fromarray(box).save(tmp_path / "boat.png"); Image.fromarray(water).save(tmp_path / "water.png")
    regs = {"asset:boat": str(tmp_path / "boat.png"), "water": str(tmp_path / "water.png")}
    plain = CP.check(str(tmp_path / "t.png"), str(tmp_path / "r.png"), regs)
    own = CP.check(str(tmp_path / "t.png"), str(tmp_path / "r.png"), regs, {"asset:boat": [225 / 255, 224 / 255, 223 / 255]})
    assert own["regions"]["asset:boat"]["delta_e"] < 1.0 and own["regions"]["asset:boat"]["target"] == "#E1E0DF"
    assert plain["regions"]["asset:boat"]["delta_e"] > 20


def test_own_colors_weights_instances():
    from genai_pipeline.pipeline import _own_colors
    out = _own_colors({"a_1": {"px": 100, "rendered": [1, 0, 0]}, "a_2": {"px": 300, "rendered": [0, 0, 1]},
                       "x": {"px": 5, "rendered": [0, 1, 0]}}, {"a_1": "a", "a_2": "a"})
    assert out == {"asset:a": [0.25, 0.0, 0.75]}


def test_own_colors_prefers_petal_for_flowers():
    from genai_pipeline.pipeline import _own_colors
    roots = {"spike_1": "red_spike_flower", "tree_1": "broadleaf_tree"}
    # root_stats would be green-contaminated; materials say petal is red
    root_stats = {
        "spike_1": {"px": 2000, "rendered": [0.3, 0.5, 0.2]},
        "tree_1": {"px": 5000, "rendered": [0.4, 0.5, 0.3]},
    }
    mats = [
        {"root": "spike_1", "material": "osk_stem", "px": 1800, "rendered": [0.3, 0.5, 0.2]},
        {"root": "spike_1", "material": "osk_petal", "px": 400, "rendered": [0.85, 0.1, 0.05]},
        {"root": "tree_1", "material": "broadleaf_tree_c", "px": 5000, "rendered": [0.4, 0.5, 0.3]},
    ]
    out = _own_colors(root_stats, roots, mats)
    assert out["asset:red_spike_flower"] == [0.85, 0.1, 0.05]
    assert out["asset:broadleaf_tree"] == [0.4, 0.5, 0.3]



def test_own_colors_binds_terrain_rock_from_island_rock_mats():
    """Cliff SAM masks often include sea fringe; bind terrain_rock to rock-mat own."""
    from genai_pipeline.pipeline import _own_colors
    roots = {"isl_1": "island_terrain", "fl_1": "red_spike_flower"}
    root_stats = {
        "isl_1": {"px": 5000, "rendered": [0.37, 0.33, 0.21]},
        "fl_1": {"px": 200, "rendered": [0.2, 0.5, 0.2]},
    }
    mats = [
        {"root": "isl_1", "material": "osk_rock", "px": 3000, "rendered": [0.45, 0.33, 0.21]},
        {"root": "isl_1", "material": "osk_rock_alt", "px": 1500, "rendered": [0.57, 0.40, 0.22]},
        {"root": "isl_1", "material": "osk_grass", "px": 500, "rendered": [0.25, 0.45, 0.20]},
        {"root": "fl_1", "material": "petal", "px": 180, "rendered": [0.67, 0.37, 0.30]},
        {"root": "fl_1", "material": "stem", "px": 20, "rendered": [0.2, 0.5, 0.2]},
    ]
    out = _own_colors(root_stats, roots, mats)
    assert "terrain_rock" in out
    # rock-only weighted, not grass
    assert abs(out["terrain_rock"][0] - (3000 * 0.45 + 1500 * 0.57) / 4500) < 1e-3
    assert "terrain" not in out  # leave SAM terrain unbound (dominant_median trap)
    assert out["asset:red_spike_flower"][0] > 0.5  # petal, not green stem



def test_asset_level_fix_scales_all_materials_toward_target():
    from genai_pipeline.pipeline import _asset_level_fix
    new = _asset_level_fix({"dock": {"osk_wood": [1.0, 1.0, 1.0]}},
                           {"dock": {"target": "#ECA586", "render": "#A0734D", "delta_e": 23.7}},
                           {"dock": {"osk_wood", "osk_post"}})
    assert set(new["dock"]) == {"osk_wood", "osk_post"}
    assert all(f > 1.0 for f in new["dock"]["osk_wood"]) and new["dock"]["osk_wood"] == new["dock"]["osk_post"]


def test_color_fix_step_uses_target_pixels_for_backdrop():
    from genai_pipeline.pipeline import _color_fix_step
    mats = [{"root": "sea_1", "material": "s10", "px": 9000, "authored": [0.45, 0.62, 0.75],
             "rendered": [0.45, 0.62, 0.75], "target": [0.22, 0.45, 0.6]},
            {"root": "rock_1", "material": "r", "px": 9000, "authored": [0.8, 0.6, 0.5], "rendered": [0.8, 0.6, 0.5]}]
    new, _ = _color_fix_step(mats, {"sea_1": "sea", "rock_1": "rock"}, {}, backdrops={"sea"}, damping=1.0)
    assert all(f < 0.8 for f in new["sea"]["s10"])          # 已经渲成设定色，但比目标图同位置浅：要压暗
    assert all(abs(f - 1) < 0.01 for f in new["rock"]["r"])


def test_backdrop_strip_ratios_are_smoothed():
    from genai_pipeline.pipeline import _color_fix_step
    mats = [{"root": "sea_1", "material": f"sea_s{i}", "px": 1000, "authored": [0.5, 0.5, 0.5],
             "rendered": [0.5, 0.5, 0.5], "target": t} for i, t in
            enumerate([[0.5, 0.5, 0.5], [0.25, 0.25, 0.25], [0.5, 0.5, 0.5]])]
    new, _ = _color_fix_step(mats, {"sea_1": "sea"}, {}, backdrops={"sea"}, damping=1.0)
    mid, edge = new["sea"]["sea_s1"][0], new["sea"]["sea_s0"][0]
    assert edge < 1.0 and mid > 0.5    # 中间那条的跳变被邻条摊开


def test_tree_prop_shapes_fit_size():
    from genai_pipeline.oskar import props as P
    for shape in ("cypress", "round", "cluster"):
        b = P.build({"type": "tree", "size_m": [0.8, 0.8, 1.4], "shape": shape, "trunk_frac": 0.15,
                     "colors": {"canopy": "#729843"}})
        vs = [v for part in b["parts"].values() for v in part["v"]]
        assert "canopy" in b["parts"] and b["palette"]["canopy"] == "#729843"
        assert max(v[2] for v in vs) <= 1.4 + 1e-6 and min(v[2] for v in vs) >= -1e-6
        assert max(abs(v[0]) for v in vs) <= 0.8 / 2 + 1e-6 or shape == "cluster"


def test_engine_choice_accepts_tree():
    from genai_pipeline.pipeline import _engine_choice_error, _engine_prop_spec
    ch = {"type": "tree", "tree": {"shape": "cypress", "trunk_frac": 0.2, "colors": {"canopy": 0}}}
    assert _engine_choice_error(ch) is None
    sp = _engine_prop_spec(ch, {"size_m": [0.75, 0.75, 1.4]}, {"swatches": [{"hex": "#729843", "share": 0.8}]})
    assert sp["type"] == "tree" and sp["shape"] == "cypress" and sp["colors"]["canopy"] == "#729843"


def test_dominant_median_ignores_dark_gaps():
    import numpy as np
    from genai_pipeline.color_palette import _dominant_median
    px = np.array([[0.93, 0.65, 0.53]] * 70 + [[0.2, 0.12, 0.08]] * 30)
    assert abs(_dominant_median(px)[0] - 0.93) < 1e-6


def test_pier_wood_alt_follows_measured_wood():
    from genai_pipeline.pipeline import _engine_prop_spec
    sp = _engine_prop_spec({"type": "pier", "pier": {"shape": "L", "colors": {"wood": 0}}}, {"size_m": [7, 7, 1.4]},
                           {"swatches": [{"hex": "#F9B888", "share": 0.4}]})
    assert sp["colors"]["wood_alt"] == "#E0A67A"


def test_body_size_excludes_backdrop():
    from genai_pipeline.pipeline import _body_size
    comp = {"instances": {"sea_1": {"min": [-110, -110, -70], "max": [110, 110, 19]},
                          "isl_1": {"min": [-8, -8, 0], "max": [8, 8, 5]}, "lh_1": {"min": [2, 4, 4], "max": [6, 8, 18]}}}
    assert _body_size(comp, [{"id": "sea_1", "backdrop": True}, {"id": "isl_1"}, {"id": "lh_1"}]) == [16, 16, 18]


def test_color_fix_dino_ok_accepts_mild_dino_drop_when_de_improves():
    """ΔE 明显下降时，允许更大的 DINO 回落；基线本就不像时也不挡颜色。"""
    from genai_pipeline.pipeline import _color_fix_dino_ok
    # 旧门：tol=0.01 会拒；新门：ΔE 降 ≥1 → tol 0.05，应接受
    assert _color_fix_dino_ok(24.1, 25.5, 0.268, 0.31, dino_tol=0.01, dino_tol_when_de=0.05)
    # 资产级：25.5→22.1，DINO 0.263 vs 0.31
    assert _color_fix_dino_ok(22.1, 25.5, 0.263, 0.31, dino_tol=0.01, dino_tol_when_de=0.05)
    # 基线 DINO < 0.35 且 ΔE 改善 ≥0.5：即使 DINO 掉很多也接受
    assert _color_fix_dino_ok(24.5, 25.5, 0.20, 0.31, dino_tol=0.01, dino_tol_when_de=0.05)
    # ΔE 没有实质改善：仍拒绝
    assert not _color_fix_dino_ok(25.4, 25.5, 0.31, 0.31, dino_tol=0.01, dino_tol_when_de=0.05)
    # 基线已经很像（DINO≥0.35）且 ΔE 只微降：仍要求 DINO 不掉过 tol
    assert not _color_fix_dino_ok(25.2, 25.5, 0.30, 0.40, dino_tol=0.01, dino_tol_when_de=0.05)
    assert _color_fix_dino_ok(25.2, 25.5, 0.395, 0.40, dino_tol=0.01, dino_tol_when_de=0.05)


def test_estate_prop_has_tower_house_and_arch():
    from genai_pipeline.oskar import props as P
    out = P.build({"type": "estate", "size_m": [8.0, 12.0, 6.0],
                   "colors": {"plaster": "#CAA279", "roof": "#B67C40", "stone": "#9C8F84"}})
    assert {"plaster", "roof", "stone"}.issubset(out["parts"])
    assert "trim" in out["parts"] or "door" in out["parts"]  # 拱门 / 门
    vs = [v for part in out["parts"].values() for v in part["v"]]
    assert max(v[2] for v in vs) <= 6.0 + 1e-3
    # 塔在后（+y）、屋/拱在前（-y）：y 跨度应接近深度
    assert max(v[1] for v in vs) - min(v[1] for v in vs) > 6.0


def test_engine_prop_spec_estate():
    from genai_pipeline.pipeline import _engine_prop_spec, _engine_choice_error
    a = {"id": "stone_estate", "name": "石砌庄园（塔+石屋+拱门）",
         "description": "中央方形石塔带橙红陶瓦四坡尖顶，前方长石屋带拱门",
         "size_m": [7.8, 11.8, 6.1], "sam_prompt": "stone villa tower"}
    pm = {"base": "#CAA279", "swatches": [{"hex": "#CAA279"}, {"hex": "#B67C40"}, {"hex": "#7F502F"}]}
    choice = {"type": "estate", "estate": {"wall_frac": 0.55, "arch": True, "wing": True,
                                           "colors": {"plaster": 0, "roof": 1, "stone": 2}}}
    assert _engine_choice_error(choice) is None
    spec = _engine_prop_spec(choice, a, pm)
    assert spec["type"] == "estate" and spec["arch"] and spec["colors"]["roof"] == "#B67C40"


def test_tree_canopy_uses_the_llm_chosen_measured_cluster():
    """颜色只认 LLM 按编号选的实测色团，不按名字（「红」「花」）改写。"""
    from genai_pipeline.pipeline import _engine_prop_spec
    a = {"id": "red_spike_flower", "name": "红色高穗花", "description": "深红穗状花", "size_m": [2.0, 2.0, 4.0]}
    pm = {"base": "#234E6A", "clusters": [{"hex": "#234E6A", "share": 0.5},
                                          {"hex": "#AD402F", "share": 0.3},
                                          {"hex": "#847B54", "share": 0.2}]}
    choice = {"type": "tree", "tree": {"shape": "cypress", "trunk_frac": 0.12, "count": 3,
                                       "colors": {"canopy": 1, "trunk": 2}}}
    spec = _engine_prop_spec(choice, a, pm)
    assert spec["colors"]["canopy"] == "#AD402F" and spec["colors"]["trunk"] == "#847B54"


def test_flower_prop_shapes():
    from genai_pipeline.oskar import props as P
    for shp in ("tulip", "spike", "blossom", "daisy", "foliage"):
        out = P.build({"type": "flower", "size_m": [0.6, 0.6, 1.0], "shape": shp,
                       "colors": {"petal": "#B43E2D", "stem": "#5A6B3E", "leaf": "#4F8A55", "center": "#E8C84A"}})
        assert out["parts"], shp
        if shp == "foliage":
            assert "leaf" in out["parts"]
        else:
            assert "petal" in out["parts"] or "center" in out["parts"]


def test_flower_engine_spec_keeps_measured_colours():
    from genai_pipeline.pipeline import _engine_prop_spec, _engine_choice_error
    a = {"id": "red_tulip", "name": "杯状红郁金香", "description": "红色郁金香", "size_m": [0.5, 0.5, 0.8]}
    pm = {"base": "#B43E2D", "clusters": [{"hex": "#B43E2D", "share": 0.6}, {"hex": "#843428", "share": 0.3},
                                          {"hex": "#2E3E37", "share": 0.1}]}
    choice = {"type": "flower", "flower": {"shape": "tulip", "stem_frac": 0.3, "colors": {"petal": 0, "stem": 2}}}
    assert _engine_choice_error(choice) is None
    spec = _engine_prop_spec(choice, a, pm)
    assert spec["type"] == "flower" and spec["shape"] == "tulip"
    assert spec["colors"]["petal"] == "#B43E2D" and spec["colors"]["stem"] == "#2E3E37"
    assert spec["colors"]["leaf"] == "#843428"          # 没选的部件按占比顺序补实测色
    # 深色叶子保持实测色，不被提亮成预设绿
    leaf = _engine_prop_spec({"type": "flower", "flower": {"shape": "foliage", "colors": {"leaf": 2}}}, a, pm)
    assert leaf["colors"]["leaf"] == "#2E3E37"
