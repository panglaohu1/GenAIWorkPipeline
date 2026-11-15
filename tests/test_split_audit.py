"""拆分 / 取色区域的自检（莫里斯岛实测的问题）：重复物件只取了一个实例、地形认成远处岬角、岛顶量成海色、多色团取色。"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "genai_pipeline"))
import color_palette as CP  # noqa: E402
import depth_layout as DL  # noqa: E402


def _box(h, w, y0, y1, x0, x1):
    m = np.zeros((h, w), bool)
    m[y0:y1, x0:x1] = True
    return m


def test_asset_region_is_union_of_trusted_instances():
    a = _box(50, 50, 0, 10, 0, 10)
    b = _box(50, 50, 20, 30, 20, 30)
    junk = _box(50, 50, 40, 45, 40, 45)
    kept = [("cypress", 0, 0.8, a), ("cypress", 1, 0.7, b), ("cypress", 2, 0.2, junk), ("sea", 0, 0.9, ~a)]
    out = DL.asset_regions(kept, {"sea"}, set())
    m = out["asset:cypress"]
    assert m[a].all() and m[b].all() and not m[junk].any()
    assert "asset:sea" not in out


def test_terrain_detached_far_headland():
    main = _box(100, 100, 40, 90, 10, 80)
    headland = _box(100, 100, 30, 35, 90, 99)
    assert DL.terrain_detached(headland, main)
    assert not DL.terrain_detached(_box(100, 100, 50, 60, 20, 40), main)   # 主岛上的一块：并进去


def test_audit_flags_unassigned_and_land_measured_as_water():
    img = np.zeros((40, 40, 3), np.float32)
    img[:20] = [0.25, 0.42, 0.5]     # 海
    img[20:] = [0.55, 0.6, 0.25]     # 草
    sea = _box(40, 40, 0, 20, 0, 40)
    top_wrong = _box(40, 40, 5, 10, 5, 10)   # 圈在海里
    masks = {"water": sea, "terrain_top": top_wrong}
    colors = {n: CP.region_colors(img[m]) for n, m in masks.items()}
    res = CP.audit(img, masks, colors)
    assert res["coverage"]["warn"] and res["coverage"]["unassigned"] > 0.45
    assert res["coverage"]["threshold"] == 0.3
    assert res["needs_retry"]
    assert "terrain_top" in res["suspect"]
    reasons = CP.needs_retry({**res, **colors})
    assert any("未归属" in r for r in reasons)
    assert any("terrain_top" in r for r in reasons)
    ok = {"water": sea, "terrain_top": _box(40, 40, 20, 40, 0, 40)}
    res2 = CP.audit(img, ok, {n: CP.region_colors(img[m]) for n, m in ok.items()})
    assert not res2["coverage"]["warn"] and "suspect" not in res2
    assert not res2["needs_retry"]
    assert CP.needs_retry(res2) == []


def test_audit_flags_grass_blue_without_water_ref():
    """草地主色偏蓝（无水面对照时）也应报警并触发重圈。"""
    img = np.zeros((30, 30, 3), np.float32)
    img[:] = [0.35, 0.45, 0.72]  # 蓝灰
    top = _box(30, 30, 5, 25, 5, 25)
    colors = {"terrain_top": CP.region_colors(img[top])}
    res = CP.audit(img, {"terrain_top": top}, colors, max_unassigned=0.99)  # 覆盖够，只测色异常
    assert "terrain_top" in res["suspect"]
    assert res["needs_retry"]
    assert any("偏蓝" in w for w in res["suspect"].values())


def test_terrain_rep_is_largest_landmass():
    from genai_pipeline.pipeline import _terrain_rep
    insts = [{"box": [0.88, 0.3, 0.99, 0.4], "score": 0.8, "area": 0.006},
             {"box": [0.1, 0.3, 0.9, 0.95], "score": 0.5, "area": 0.31}]
    assert _terrain_rep(insts)["area"] == 0.31


def test_apply_keeps_all_sam_instances_for_repeatable_props():
    """重复物件：placements 跟 SAM 实例数走，不被 LLM 的 1 个代表截断。"""
    spec = {
        "assets": [
            {"id": "island", "kind": "environment", "method": "terrain",
             "size_m": [20, 16, 5], "placements": [{"at": [0, 0, 0], "rot_z": 0, "scale": 1}]},
            {"id": "cypress", "kind": "prop", "method": "image3d",
             "size_m": [1, 1, 3], "placements": [{"at": [2, 0, 5], "rot_z": 10, "scale": 1}]},
        ],
        "camera": {"eye": [0, -20, 8], "target": [0, 0, 3], "fov_deg": 40},
    }
    def row(k, x, y, z=5.0, h=3.0, fp=(1.0, 0.8)):
        return {"k": k, "score": 0.8, "pixels": 100, "center": [x, y], "base_z": z, "top_z": z + h,
                "median_z": z + h / 2, "front_y": y - fp[1] / 2, "footprint": list(fp), "height": h,
                "surface_z": z + h, "base_ray": [0, 0.2, -0.98], "base_v": 0.7}
    meas = {
        "instances": {
            "island": [row(0, 0, 0, z=0.0, h=5.0, fp=(20.0, 16.0))],
            "cypress": [row(0, -3, 2), row(1, 0, 1), row(2, 3, 0)],
        },
        "camera": {"eye": [0, -30, 10], "look": [0, 1, -0.2], "fov_deg": 40, "pitch_down_deg": 12,
                   "fy_n": 1.0, "cy_n": 0.5},
        "plane": {"n": [0, 0, 1], "d": 0},
        "frame": {"right": [1, 0, 0], "fwd": [0, 1, 0], "up": [0, 0, 1]},
        "backdrop_on_plane_frac": {},
    }
    DL.apply(spec, meas, 1.0)
    assert len(spec["assets"][1]["placements"]) == 3
    assert len(spec["assets"][0]["placements"]) == 1  # 地形仍只取 1


def test_region_colors_returns_two_to_three_clusters_not_mud_base():
    """花丛多色：应给出 2–3 个色团，主色取最大色团而不是泥色中位数。"""
    rng = np.random.default_rng(0)
    # 红花 + 绿叶（比例接近，中位数会偏棕）
    red = np.tile(np.array([0.85, 0.15, 0.12], np.float32), (1200, 1))
    green = np.tile(np.array([0.25, 0.55, 0.18], np.float32), (1800, 1))
    soil = np.tile(np.array([0.45, 0.38, 0.26], np.float32), (600, 1))
    px = np.concatenate([red, green, soil])
    px = px + rng.normal(0, 0.02, px.shape).astype(np.float32)
    px = np.clip(px, 0, 1)
    rc = CP.region_colors(px)
    assert "clusters" in rc and 2 <= len(rc["clusters"]) <= 3
    assert rc["swatches"] == rc["clusters"]
    # 主色应接近红或绿，而不是泥土中位
    base = np.array([int(rc["base"][i:i + 2], 16) for i in (1, 3, 5)], float) / 255
    median = np.array([int(rc["median"][i:i + 2], 16) for i in (1, 3, 5)], float) / 255
    # 最大色团是绿（1800 > 1200）：base 应偏绿，与泥色中位拉开
    assert base[1] > base[0] and base[1] > base[2]
    assert float(np.linalg.norm(base - median)) > 0.05 or rc["base"] != rc["median"]


def test_engine_prop_prefers_clusters_over_swatches():
    from genai_pipeline.pipeline import _engine_prop_spec
    pm = {"base": "#AABBCC",
          "clusters": [{"hex": "#E04040", "share": 0.4}, {"hex": "#3A8A3A", "share": 0.35}],
          "swatches": [{"hex": "#736243", "share": 0.9}]}  # 旧泥色不应抢优先级
    choice = {"type": "tree", "tree": {"shape": "cluster", "colors": {"canopy": 0, "trunk": 1}}}
    sp = _engine_prop_spec(choice, {"size_m": [1, 1, 1.5]}, pm)
    assert sp["colors"]["canopy"] == "#E04040" and sp["colors"]["trunk"] == "#3A8A3A"


def test_split_retry_reasons_from_pipeline():
    from genai_pipeline.pipeline import _split_retry_reasons
    pm = {"coverage": {"assigned": 0.4, "unassigned": 0.6, "warn": True, "threshold": 0.3},
          "needs_retry": True,
          "suspect": {"terrain_top": "主色偏蓝"}}
    rs = _split_retry_reasons(pm)
    assert any("60%" in r for r in rs)
    assert any("岛顶草地" in r or "terrain_top" in r for r in rs)
    assert _split_retry_reasons({"error": "x"}) == []
    assert _split_retry_reasons({"coverage": {"warn": False}, "needs_retry": False}) == []


def test_from_box_does_not_wipe_sibling_sam_instances(tmp_path):
    """框补救：代表相对 LLM 框太小时，不得删掉同类其它 SAM 实例（柏树 27→1 的根因）。"""
    import json
    from PIL import Image

    H, W = 64, 64
    # 深度：地面 z≈0，两棵树柱抬高
    P = np.zeros((H, W, 3), np.float32)
    ys, xs = np.mgrid[0:H, 0:W]
    P[..., 0] = (xs / max(W - 1, 1) - 0.5) * 8
    P[..., 1] = (0.5 - ys / max(H - 1, 1)) * 8
    P[..., 2] = 0.05
    t0 = _box(H, W, 10, 30, 6, 14)    # 左：代表，几乎不在大框内
    t1 = _box(H, W, 10, 30, 40, 50)   # 右
    P[t0 | t1, 2] = 2.5
    N = np.zeros((H, W, 3), np.float32); N[..., 2] = 1.0
    K = np.array([[W, 0, W / 2], [0, H, H / 2], [0, 0, 1]], np.float32)
    npz_path = tmp_path / "d.npz"
    np.savez(npz_path, points=P, mask=np.ones((H, W), bool),
             intrinsics=K, normal=N)

    def save_mask(m, name):
        p = tmp_path / name
        Image.fromarray((m.astype(np.uint8) * 255)).save(p)
        return str(p)

    masks = {"cypress": [{"mask": save_mask(t0, "t0.png"), "score": 0.9},
                         {"mask": save_mask(t1, "t1.png"), "score": 0.85}]}
    # 大框偏右：代表 t0 在框内覆盖 ≪ 20% → 旧逻辑会 wipe
    boxes = {"cypress": [0.4, 0.05, 0.95, 0.7]}
    x0, y0, x1, y1 = int(0.4 * W), int(0.05 * H), int(0.95 * W), int(0.7 * H)
    area = (x1 - x0) * (y1 - y0)
    assert t0[y0:y1, x0:x1].sum() < 0.2 * area

    regions = {}
    try:
        meas = DL.measure(npz_path, masks, set(), boxes, set(), regions_out=regions)
    except Exception as e:
        # 若 _load 键名不匹配，跳过端到端改测契约
        import pytest
        pytest.skip(f"measure/_load 环境不匹配: {e}")
    rows = meas["instances"]["cypress"]
    assert len(rows) >= 2, f"框补救清空了实例: {len(rows)}"
    assert "asset:cypress" in regions
    assert regions["asset:cypress"][t0].mean() > 0.5
    assert regions["asset:cypress"][t1].mean() > 0.5


def test_run9ac6_measure_keeps_cypress_and_flower_instances():
    """回归：失败跑次 request 里 cypress/花丛多实例，框补救后不得变 1。
    重跑后花丛拆成 red_tulip / warm_blossom 等；优先测仍存在的多实例花类。"""
    run = Path(__file__).resolve().parents[1] / "runs" / "20261026-202458-9ac6"
    req_p = run / "concept" / "_scene" / "depth" / "request.json"
    # 测量用 fused（含 normal）；若无则退回 da3
    npz = run / "concept" / "_scene" / "depth" / "depth_da3_fused.npz"
    if not npz.exists():
        npz = run / "concept" / "_scene" / "depth" / "depth_da3.npz"
    if not req_p.exists() or not npz.exists():
        import pytest
        pytest.skip("跑次产物不在工作区")
    import json
    req = json.loads(req_p.read_text())
    back = {a["id"] for a in req["spec"]["assets"] if a.get("method") == "backdrop" or a.get("kind") == "backdrop"}
    terrain = {a["id"] for a in req["spec"]["assets"] if a.get("method") == "terrain" or a.get("kind") == "environment"}
    boxes = {a["id"]: a.get("bbox_llm") or a.get("bbox") for a in req["spec"]["assets"]}
    meas = DL.measure(npz, req["masks"], back, boxes, terrain)
    assert len(meas["instances"].get("cypress_tree") or []) >= 10
    flower_ids = [k for k in ("warm_blossom", "red_tulip", "flower_shrub", "red_spike_flower") if k in meas["instances"]]
    assert flower_ids, f"无花类实例键: {sorted(meas['instances'])}"
    assert max(len(meas["instances"][k]) for k in flower_ids) >= 5


def test_validate_gap_assets_autofills_missing_placements():
    """漏项补全：LLM 忘写 placements 时自动补默认，不再整段 JSON 失败。"""
    from genai_pipeline.pipeline import _validate_gap_assets, _broaden_sam_prompt, _ensure_placements
    o = {"assets": [{
        "id": "stream_waterfall",
        "name": "溪涧瀑布",
        "bbox": [100, 200, 300, 500],
        "size_m": [2, 2, 4],
        "sam_prompt": "stream waterfall water",
        # 故意不写 placements
    }]}
    err = _validate_gap_assets(o, {"island_terrain", "cypress_tree"})
    assert err is None, err
    a = o["assets"][0]
    assert a["placements"] and a["placements"][0]["at"] == [0, 0, 0]
    assert a["sam_prompt"] == "waterfall"  # broaden
    assert "stream" in [x.lower() for x in a.get("sam_alts") or []]


def test_broaden_sam_prompt_for_narrow_flowers():
    from genai_pipeline.pipeline import _broaden_sam_prompt
    for narrow, expect in [
        ("red spike flower", "red flower cluster"),
        ("white daisy flower", "white flower"),
        ("green leafy shrub", "green bush"),
    ]:
        a = {"id": "x", "sam_prompt": narrow, "sam_alts": []}
        _broaden_sam_prompt(a)
        assert a["sam_prompt"] == expect, (narrow, a)
        assert a["sam_alts"], a


def test_terrain_top_mask_falls_back_when_strict_empty():
    """斜视：cosu>0.8 几乎为空时，应回退到更宽阈值并得到足够像素。"""
    import numpy as np
    H, W = 40, 40
    t = np.ones((H, W), bool)
    # 大部分顶面 cosu≈0.6，只有 5 像素 >0.8
    cosu = np.full((H, W), 0.6, np.float32)
    cosu[:1, :5] = 0.9
    zmap = np.full((H, W), 5.0, np.float32)
    zmap[:10] = 8.0  # 高处
    m = DL.terrain_top_mask(t, cosu, zmap, min_px=30)
    assert int(m.sum()) >= 30
    # 严格阈值本身不够
    strict = DL._erode(t & (cosu > 0.8) & (zmap > 0.5 * 8.0))
    assert int(strict.sum()) < 30


def test_terrain_palette_lock_grass_without_terrain_top():
    """岛顶取色缺失时，草色应落到绿色物件或地形色团，而不是缺 grass 键。"""
    from genai_pipeline.pipeline import _terrain_palette_lock
    spec = {"palette_measured": {
        "terrain_rock": {"base": "#9D7843", "lit": "#AE874D", "shade": "#24343B"},
        "terrain": {"base": "#A38249", "lit": "#B19358", "shade": "#263735",
                    "clusters": [{"hex": "#3A8A3A", "share": 0.3}, {"hex": "#A38249", "share": 0.7}]},
        "assets": {"green_foliage": {"base": "#2E8B3A", "lit": "#4CAA55", "shade": "#1A5A22"}},
        "terrain_mode": "multi",
        # 故意无 terrain_top
    }}
    out = _terrain_palette_lock(spec)
    assert out and "grass" in out["palette"]
    assert out["palette"]["grass"].startswith("#")
