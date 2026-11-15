"""把多个场景串到一颗小行星上（oskar/planet.py，纯 Python）。"""
import math

from genai_pipeline.oskar import planet as PL

SIZES = {"a": [64, 55, 25], "b": [72, 62, 34], "c": [64, 55, 28], "d": [64, 55, 30], "e": [64, 55, 24]}


def test_azimuthal_mapping_keeps_heights_and_distances():
    fr = PL.frame(10, 40, 15)
    R = 50.0
    c = PL.to_sphere((0, 0, 0), fr, R)
    assert abs(math.dist(c, (0, 0, 0)) - R) < 1e-6
    p = PL.to_sphere((12.0, -5.0, 3.0), fr, R)
    assert abs(math.dist(p, (0, 0, 0)) - (R + 3.0)) < 1e-6              # 高度 → 半径
    q = PL.to_sphere((12.0, -5.0, 0.0), fr, R)
    ang = math.acos(sum(x * y for x, y in zip(c, q)) / R / R)
    assert abs(ang * R - 13.0) < 1e-6                                     # 平面距离 → 球面弧长（等距）
    e, n = fr[1], fr[2]
    assert abs(sum(x * y for x, y in zip(e, n))) < 1e-9 and abs(sum(x * y for x, y in zip(e, fr[0]))) < 1e-9


def test_plan_orders_scenes_along_journey_and_lifts_climax():
    order = ["a", "b", "c", "d", "e"]
    lay = PL.plan(order, SIZES, climax="c")
    lons = [s["lon"] for s in lay["scenes"]]
    assert lons == sorted(lons) and [s["id"] for s in lay["scenes"]] == order
    # 相邻两站在球面上的弧长足够放下两座岛
    for a, b in zip(lay["scenes"], lay["scenes"][1:]):
        ca, cb = PL.unit(a["lat"], a["lon"]), PL.unit(b["lat"], b["lon"])
        arc = math.acos(max(-1, min(1, sum(x * y for x, y in zip(ca, cb))))) * lay["radius"]
        assert arc >= a["r"] + b["r"] - 1e-6
    base = PL.plan(order, SIZES)
    assert lay["scenes"][2]["lat"] > base["scenes"][2]["lat"]
    assert [l[:2] for l in lay["links"][:4]] == [["a", "b"], ["b", "c"], ["c", "d"], ["d", "e"]]
    # 单站不能弯得太厉害：岛缘角距离 ≤ 约 36°
    assert max(s["r"] for s in lay["scenes"]) / lay["radius"] <= math.radians(36.1)


def test_links_and_ribbons_bridge_neighbouring_islands():
    lay = PL.plan(["a", "b", "c"], SIZES)
    by = {s["id"]: s for s in lay["scenes"]}
    line = PL.link_path(by["a"], by["b"], lay["radius"])
    assert len(line) >= 3
    rb = PL.ribbon(line, lay["radius"])
    assert rb["grass"] and rb["path"] and len(rb["rock"]) == 2 * len(rb["grass"])
    top = [math.dist(p, (0, 0, 0)) for q in rb["grass"] for p in q]
    assert min(top) > lay["radius"]                                       # 陆桥顶面在海面以上


def test_face_rotation_turns_scene_toward_camera_and_ops_are_bounded():
    lay = PL.plan(["a", "b", "c"], SIZES)
    s = lay["scenes"][1]
    M = PL.face_rotation(s)
    c = PL.unit(s["lat"], s["lon"])
    rc = [sum(M[i][j] * c[j] for j in range(3)) for i in range(3)]
    view = (0.55, -1.5, 0.42)
    vl = math.sqrt(sum(v * v for v in view))
    assert sum(rc[i] * view[i] / vl for i in range(3)) > 0.3               # 转动后法向朝向相机一侧
    assert abs(sum(rc[i] ** 2 for i in range(3)) - 1) < 1e-4   # 矩阵按 6 位小数传给 Blender
    st, log = PL.apply_ops({"order": ["a", "b", "c"]}, [{"op": "spacing", "factor": 5}, {"op": "nudge", "scene": "b", "dx": 50},
                                                        {"op": "swap", "a": "a", "b": "c"}], ["a", "b", "c"])
    assert st["spacing"] == 1.8 and st["nudges"]["b"] == [20, 0] and st["order"] == ["a", "b", "c"]


def test_journey_wraps_walk_points_onto_the_planet():
    lay = PL.plan(["a", "b"], SIZES)
    recs = {"a": {"walk": [[0, 0, 4]], "paths": [[[0, 0, 2], [3, 0, 4]]]}, "b": {"walk": [[2, 2, 2]], "paths": []}}
    walk, paths, stops = PL.journey(recs, lay, {"a": {"name": "港"}, "b": {"name": "城"}})
    R = lay["radius"]
    assert abs(math.dist(walk[0], (0, 0, 0)) - (R + 4)) < 1e-2
    assert len(paths) == 2 and stops[0]["name"] == "港" and stops[1]["entry"] == walk[1]


def _ring(r, z, n=40):
    return [[r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), z] for i in range(n)] + [[0, 0, z]]


def test_terrain_links_read_both_rims_and_pick_landform():
    from genai_pipeline.oskar import program as OP
    lay = PL.plan(["a", "b", "c"], SIZES)
    walks = {"a": _ring(16, 4.0), "b": _ring(18, 16.0), "c": _ring(15, 14.0)}
    specs = PL.link_specs(lay, walks)
    assert [s["id"] for s in specs] == ["link_a__b", "link_b__c"]
    ab, bc = specs
    assert abs(ab["edge_a"] - 16) < 1.5 and abs(ab["edge_b"] - 18) < 1.5
    assert ab["h_a"] == 3 and ab["h_b"] == 9                      # 4 米 → 第 3 层，16 米 → 第 9 层（海面第 1 层）
    assert PL.default_link(ab)["kind"] == "terraces" and PL.default_link(bc)["kind"] == "ridge"
    # 连接自己的切平面：中心在间隙正中，本地 +x 指向 B
    fr = PL.frame(ab["lat"], ab["lon"], ab["yaw"])
    cb = PL.unit(*[(s["lat"], s["lon"]) for s in lay["scenes"] if s["id"] == "b"][0])
    assert sum(fr[1][i] * cb[i] for i in range(3)) > 0
    for kind in PL.LINK_KINDS:
        prog = PL.link_program(ab, {"kind": kind, "width": 12})
        assert OP.validate_program(prog) is None and prog["_link"]["kind"] == kind
        st = OP.build_state(prog)
        assert st.paths and st.cells
        # 两端台地的顶面对齐岛缘高度
        ends = [it for it in prog["items"][:2]]
        assert ends[0]["top"] == ab["h_a"] and ends[1]["top"] == ab["h_b"]
    ridge = PL.link_program(bc, {"kind": "ridge", "saddle": -1})
    assert ridge["_link"]["saddle"] < min(bc["h_a"], bc["h_b"])
    st, log = PL.apply_ops({"order": ["a", "b", "c"]}, [{"op": "link", "a": "c", "b": "b", "kind": "isthmus", "width": 40},
                                                        {"op": "link", "a": "a", "b": "c", "kind": "ridge"}], ["a", "b", "c"])
    assert st["link_overrides"] == {"link_b__c": {"kind": "isthmus", "width": 18.0}} and "不相邻" in log[1]


def test_journey_uses_terrain_link_paths_instead_of_bridge_centerlines():
    lay = PL.plan(["a", "b"], SIZES)
    recs = {"a": {"walk": [[0, 0, 4]], "paths": []}, "b": {"walk": [[2, 2, 2]], "paths": []}}
    specs = PL.link_specs(lay, {"a": _ring(16, 4.0), "b": _ring(16, 4.0)})
    link = {**specs[0], "walk": [[0, 0, 4.0]], "paths": [[[-6, 0, 4.0], [6, 0, 4.0]]]}
    walk, paths, _ = PL.journey(recs, lay, {}, links=[link])
    R = lay["radius"]
    assert len(paths) == 1 and abs(math.dist(walk[-1], (0, 0, 0)) - (R + 4)) < 1e-2
