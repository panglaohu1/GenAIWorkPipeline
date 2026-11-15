"""图生 3D 场景流水线（scene3d）：资产分派、轮数配置、质量验收、服务登记。"""
from genai_pipeline import pipeline, quality, services, agent_config


def test_scene3d_mode_registered():
    assert pipeline.STAGES["scene3d"] == ["decompose", "model", "compose", "deliver"]
    assert "shape3d" in services.manager.services
    assert "shape3d" in agent_config.TOOLS


def test_asset_method_routes_assets_terrain_and_backdrop():
    chair = {"kind": "prop", "size_m": [0.6, 0.5, 1.0]}
    ground = {"kind": "environment", "size_m": [30, 30, 1.4]}
    small_env = {"kind": "environment", "size_m": [0.8, 0.8, 0.4]}
    clouds = {"kind": "backdrop", "size_m": [120, 80, 4]}
    assert pipeline.asset_method(chair)[0] == "image3d"
    assert pipeline.asset_method(small_env)[0] == "image3d"
    assert pipeline.asset_method(ground)[0] == "terrain"
    assert pipeline.asset_method(clouds)[0] == "backdrop"
    # Agent 关掉图生 3D 工具时，道具改走蓝图；地形与远景不受影响
    assert pipeline.asset_method(chair, ["blender", "godot"])[0] == "blueprint"
    assert pipeline.asset_method(ground, ["blender", "godot"])[0] == "terrain"
    assert pipeline.asset_method(chair, ["blender", "shape3d"])[0] == "image3d"


def test_scene3d_limits_options_override_config_and_clamp():
    d = pipeline.scene3d_limits({})
    assert d["candidates"] >= 1 and d["blueprint_rounds"] >= 1 and d["compose_rounds"] >= 1
    o = pipeline.scene3d_limits({"shape3d_candidates": 4, "model_max_rounds": 2, "compose_max_rounds": 99})
    assert o["candidates"] == 4 and o["blueprint_rounds"] == 2 and o["compose_rounds"] == 20


def test_quality_reads_image3d_observation():
    state = {"spec": {"assets": [{"id": "boy", "name": "小王子"}]},
             "models": {"boy": {"mode": "image3d", "glb": "x.glb", "best": 0,
                                "candidates": [{"k": 2}], "observation": {"score": 8.0}}},
             "compose": {"rounds": [{"score": 7.0}], "champion": 0}}
    run = {"mode": "scene3d", "options": {}}
    q = quality.assess(run, state)
    names = {c["name"]: c["passed"] for c in q["checks"]}
    assert names["小王子 · 视觉验收（图生 3D）"] is True
    assert names["场景组装验收"] is True


def test_login_sessions_survive_restart(tmp_path):
    from genai_pipeline.console.auth import LocalAuth
    a = LocalAuth(tmp_path / "auth.json")
    a.setup("tester", "a-long-password")
    token = a.login("tester", "a-long-password")
    assert LocalAuth(tmp_path / "auth.json").user(token) == "tester"   # 新实例 = 控制台重启
    a.logout(token)
    assert LocalAuth(tmp_path / "auth.json").user(token) is None


def test_reference_camera_and_backdrop():
    from PIL import Image
    cam = pipeline._normalize_camera({"eye": [0, -3, 1.2], "target": [0, 20, 1], "fov_deg": 200}, 2.5)
    assert cam["fov_deg"] == 90.0 and cam["aspect"] == 2.5
    assert pipeline._normalize_camera({"eye": [0, 0, 0], "target": [0, 0, 0]}, 1) is None
    assert pipeline._normalize_camera(None, 1) is None
    img = Image.new("RGB", (100, 40), (200, 100, 50))
    assert pipeline._backdrop_color(img) == [round(200 / 255, 4), round(100 / 255, 4), round(50 / 255, 4)]


def test_quality_terrain_and_backdrop():
    state = {"spec": {"assets": [{"id": "hill", "name": "草坡"}, {"id": "sea", "name": "云海"}]},
             "models": {"hill": {"mode": "terrain", "glb": "h.glb", "checks": [{"name": "悬空", "required": True, "passed": True}],
                                 "checks_ok": True},
                        "sea": {"mode": "backdrop", "glb": "s.glb", "backdrop": True}},
             "compose": {"rounds": [{"score": 7.0}], "champion": 0}}
    q = quality.assess({"mode": "scene3d", "options": {}}, state)
    names = {c["name"]: c["passed"] for c in q["checks"]}
    assert names["草坡 · 地形硬检查（Oskar 引擎）"] is True
    assert names["云海 · GLB 导出"] is True
    assert not any(n.startswith("云海 · 视觉") for n in names)


def test_sky_is_skipped_and_cloud_sea_is_backdrop_even_if_labelled_environment():
    sky = {"id": "sunset_sky", "name": "日落天空", "kind": "environment", "size_m": [60, 40, 18]}
    sun = {"id": "sun_disk", "name": "太阳", "kind": "environment", "size_m": [2.8, 2.8, 0.3]}
    clouds = {"id": "cloud_sea", "name": "云海", "kind": "environment", "size_m": [60, 40, 3.5]}
    meadow = {"id": "flower_meadow_patch", "name": "花草坡", "kind": "environment", "size_m": [14, 14, 1.2]}
    assert pipeline.asset_method(sky)[0] == "skip"
    assert pipeline.asset_method(sun)[0] == "skip"
    assert pipeline.asset_method(clouds)[0] == "backdrop"
    assert pipeline.asset_method(meadow)[0] == "terrain"


def test_sea_words_in_description_do_not_turn_props_into_backdrop():
    # 回归：海岛场景里码头、小船、岛体的描述都写着“海面”，曾被误判成远景层
    dock = {"id": "wooden_dock", "name": "木码头", "kind": "building", "size_m": [2, 9, 1.2],
            "description": "木码头从悬崖下伸入海面"}
    boat = {"id": "fishing_boat", "name": "小渔船", "kind": "prop", "size_m": [1.2, 3, 1],
            "sam_prompt": "small boat on ocean", "description": "停在海面上的小船"}
    island = {"id": "island_terrain", "name": "海岛地形", "kind": "environment", "size_m": [36, 36, 14],
              "description": "四周是大海的悬崖小岛"}
    sea = {"id": "ocean_surface", "name": "海面", "kind": "environment", "size_m": [120, 120, 1]}
    assert pipeline.asset_method(dock)[0] == "image3d"
    assert pipeline.asset_method(boat)[0] == "image3d"
    assert pipeline.asset_method(island)[0] == "terrain"
    assert pipeline.asset_method(sea)[0] == "backdrop"


def test_backdrop_is_pushed_beyond_near_scene_and_below_ground():
    # 回归：小王子场景的云海 140×140 米放在原点附近，把机位、草坡和角色全埋进团块
    spec = {"camera": {"eye": [4, 5, 1.5], "target": [4, -20, 1.5]}, "assets": [
        {"id": "boy", "kind": "character", "method": "image3d", "size_m": [0.9, 0.9, 1.35]},
        {"id": "hill", "kind": "environment", "method": "terrain", "size_m": [30, 30, 2]},
        {"id": "clouds", "kind": "backdrop", "method": "backdrop", "size_m": [140, 140, 4]}]}
    layout = [{"id": "boy_1", "asset": "boy", "at": [4, 0, 0]},
              {"id": "hill_1", "asset": "hill", "at": [0, 0, -2]},
              {"id": "clouds_1", "asset": "clouds", "at": [0, -35, -6]}]
    out = {x["asset"]: x for x in pipeline._place_backdrops(spec, layout)}
    cx, cy, cz = out["clouds"]["at"]
    assert cx == 4  # 沿视线（-Y）推远，近边（cy + 70）在草坡远边（y = -15）之外
    assert cy + 70 <= -15
    assert cz + 4 <= -1.0                 # 顶面在地面（z = 0）以下
    assert out["boy"]["at"] == [4, 0, 0] and out["hill"]["at"] == [0, 0, -2]


def test_layout_patch_accepts_list_valued_fields():
    # 回归：Sage-A1.5 写的布局补丁把 scale / rot_z 写成列表，旧代码 float() 报错、整轮跳过
    lay = [{"id": "house_1", "asset": "house", "at": [1, 2, 3], "rot_z": 0, "scale": 1}]
    out = pipeline._apply_layout_patch(lay, {"update": {"house_1": {"scale": [1.2, 1.2, 1.2], "rot_z": [0, 0, 30],
                                                                    "at": [4, 5]}},
                                             "add": [{"asset": "house", "at": [0, 1, 0], "scale": [2, 2, 2]}]}, {"house"})
    a, b = out
    assert a["scale"] == 1.2 and a["rot_z"] == 30 and a["at"] == [4, 5, 3]
    assert b["scale"] == 2 and b["at"] == [0, 1, 0]


def test_backdrop_measured_on_the_support_plane_sits_at_z0_around_the_scene():
    spec = {"camera": {"eye": [-2, -24, 9], "target": [0, 0, 7]},
            "assets": [{"id": "rocky_island", "kind": "environment", "method": "terrain", "size_m": [30, 28, 18]},
                       {"id": "small_boat", "size_m": [5, 2, 2]},
                       {"id": "sea_horizon", "name": "海面", "method": "backdrop", "size_m": [200, 200, 4],
                        "measured": {"source": "depth", "on_plane": True}},
                       {"id": "cloud_sea", "name": "云海", "method": "backdrop", "size_m": [60, 40, 3]}]}
    layout = [{"id": "i", "asset": "rocky_island", "at": [0, 0, 0]}, {"id": "b", "asset": "small_boat", "at": [9, -9, 0]},
              {"id": "s", "asset": "sea_horizon", "at": [3, 37, 7], "scale": 0.22},
              {"id": "c", "asset": "cloud_sea", "at": [0, 0, 0]}]
    out = {x["id"]: x for x in pipeline._place_backdrops(spec, layout)}
    assert out["s"]["at"] == [4.5, -4.5, -4.0] and out["s"]["scale"] == 1.0
    assert out["c"]["at"][1] > 20 and out["c"]["at"][2] < 9   # 云海仍推到远处、压到机位以下


def test_depth_layout_apply_uses_measurements_not_llm_sizes():
    import pytest
    pytest.importorskip("numpy")   # 深度定位在 envs/depth 里跑；控制台环境没有 numpy 时跳过
    from genai_pipeline import depth_layout as DL
    spec = {"assets": [{"id": "island", "kind": "environment", "method": "terrain", "size_m": [30, 28, 18],
                        "placements": [{"at": [0, 0, 0]}]},
                       {"id": "tower", "kind": "building", "size_m": [6, 6, 22], "placements": [{"at": [2, 1, 9], "rot_z": 15}]},
                       {"id": "boat", "kind": "prop", "size_m": [5, 2, 2], "placements": [{"at": [9, -9, 0]}]},
                       {"id": "sea", "method": "backdrop", "size_m": [200, 200, 4], "placements": [{"at": [0, 0, 0]}]}],
            "camera": {"eye": [0, -24, 9], "target": [0, 0, 7], "fov_deg": 40}}
    row = lambda c, fy, base, h, fp, mz: {"k": 0, "score": .9, "pixels": 100, "center": c, "front_y": fy, "base_z": base,
                                          "top_z": base + h, "median_z": mz, "footprint": fp, "height": h}
    meas = {"plane": {"source": "free-near", "inlier_frac": .9},
            "camera": {"eye": [0, 0, 6], "look": [0, 0.966, -0.259], "fov_deg": 57.5, "pitch_down_deg": 15},
            "instances": {"island": [row([1, 16], 16, 0.1, 6, [15, 8], 3)], "tower": [row([3, 20], 19.5, 6, 9.5, [2, 1], 10)],
                          "sea": [row([0, 10], 5, 0, 0.1, [60, 40], 0.02)]}}
    s = DL.apply(spec, meas, 1.0)
    a = {x["id"]: x for x in spec["assets"]}
    assert s["anchor"] == "island" and a["island"]["size_m"][0] == 15 and a["island"]["size_m"][2] == 6
    assert a["tower"]["size_m"][2] == 9.5 and a["tower"]["placements"][0]["rot_z"] == 15
    assert a["tower"]["placements"][0]["at"][2] == 6
    assert a["boat"]["measured"]["source"] == "llm-scaled" and a["boat"]["size_m"][0] == 2.5
    assert a["sea"]["measured"]["on_plane"] is True
    assert spec["camera"]["fov_deg"] == 57.5 and spec["camera"]["eye"][2] == 6


def test_quality_objective_metrics_are_reported_but_do_not_pass_a_low_ledger():
    """2026-10-30 用户确认：门槛只看评审分；客观 DINO/SigLIP 写进说明，不放行。"""
    state = {"spec": {"assets": [{"id": "horse", "name": "马", "poly_budget": 8000}]},
             "models": {"horse": {"mode": "image3d", "glb": "x.glb", "best": 0,
                                  "candidates": [{"k": 1, "metrics": {"dino": 0.73, "siglip": 0.77}}],
                                  "observation": {"score": 2.5}}},
             "compose": {"rounds": [{"score": 2.3, "observation": {"diffs": [
                 {"severity": 3}, {"severity": 3}, {"severity": 2}, {"severity": 2},
                 {"severity": 2}, {"severity": 2}, {"severity": 1}, {"severity": 1},
                 {"severity": 1}, {"severity": 1}, {"severity": 1}, {"severity": 1}]},
                 "metrics": {"dino": 0.34, "siglip": 0.60}}], "champion": 0}}
    q = quality.assess({"mode": "scene3d", "options": {}}, state)
    names = {c["name"]: c for c in q["checks"]}
    assert names["马 · 视觉验收（图生 3D）"]["passed"] is False
    assert "DINO 0.730" in names["马 · 视觉验收（图生 3D）"]["detail"]
    assert names["场景组装验收"]["passed"] is False
    assert q["status"] != "passed"


def test_quality_scene3d_blueprint_poly_rechecked_against_budget():
    state = {"spec": {"assets": [{"id": "rabbit", "name": "兔子", "kind": "character", "poly_budget": 8000}]},
             "models": {"rabbit": {"mode": "blueprint", "glb": "r.glb", "exported_round": 5, "best": 0,
                                   "rounds": [{"round": 5, "score": 3.75, "review": {"pass": False},
                                               "hard": ["三角面 6032 超过预算 4000"],
                                               "metrics": {"dino": 0.46, "siglip": 0.79}}]}},
             "compose": {"rounds": [{"score": 7.0, "metrics": {"dino": 0.4, "siglip": 0.6}}], "champion": 0}}
    q = quality.assess({"mode": "scene3d", "options": {}}, state)
    names = {c["name"]: c for c in q["checks"]}
    assert names["兔子 · 硬检查"]["passed"] is True
    assert names["兔子 · 视觉验收"]["passed"] is False  # 评审 3.75 < 通过线；客观分不放行
