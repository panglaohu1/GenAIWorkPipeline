"""能力层：契约 / Router / 经验 / Watchdog，三个适配器（布局、材质色表、Oskar 体素），本管线能力的计划执行；
有 Node 和 TigerInBamboo 能力目录时（GENAI_CAP_REACODE / GENAI_CAP_WORKSHOP 或 config）再跑真实的外部能力。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from genai_pipeline.capabilities import bridge, gscene as GS, objects as OBJ, voxel as CV
from genai_pipeline.capabilities.contract import (Capability, ExperienceStore, Registry, TierAgent, params_for, route,
                                                   summarize_checks, with_watchdog)

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = bridge.available()[0]
needs_external = pytest.mark.skipif(not EXTERNAL, reason="没有 Node 或 TigerInBamboo 能力目录")


# ------------------------------------------------------------------ 契约
def test_params_are_clamped_and_defaulted():
    meta = {"params": {"count": {"type": "int", "min": 1, "max": 3, "default": 1},
                       "amount": {"min": 0.05, "max": 0.35, "default": 0.15}, "flag": {"type": "bool", "default": False}}}
    assert params_for(meta, {"count": 9, "amount": -1}) == {"count": 3, "amount": 0.05, "flag": False}
    assert params_for(meta) == {"count": 1, "amount": 0.15, "flag": False}


def test_router_needs_keyword_or_pin_and_real_targets():
    caps = [{"id": "a.x", "keywords": ["石拱", "arch"]}, {"id": "b.y", "keywords": ["灯塔"]}, {"id": "c.z", "keywords": ["烟"]}]
    matches = [{"id": "a.x", "count": 3, "targets": [{"reason": "两根柱之间低两层"}]},
               {"id": "b.y", "count": 0, "targets": []}, {"id": "c.z", "count": 1, "targets": []}]
    r = route("海边架一道石拱，再加灯塔", caps, matches, selected=["c.z"])
    assert [x["id"] for x in r] == ["c.z", "a.x"]          # 灯塔没有可作用物件 → 不推荐；已启用的排前
    assert "石拱" in r[1]["why"] and "两根柱" in r[1]["why"]
    assert route("", caps, matches) == []


def test_summarize_checks_worst_wins():
    assert summarize_checks([{"status": "pass"}, {"status": "warn"}])["status"] == "warn"
    assert summarize_checks([{"status": "pass"}, {"status": "fail"}])["summary"] == "1 项不通过"
    assert summarize_checks([])["status"] == "pass"


def test_registry_rejects_bad_meta_and_reports_match_errors():
    reg = Registry("t")
    with pytest.raises(ValueError):
        reg.add(Capability({"id": "x"}, lambda s, p: []))
    reg.add(Capability({"id": "ok.a", "name": "A", "keywords": ["a"]}, lambda s, p: [{"id": 1, "score": 2, "reason": "r"}]))
    reg.add(Capability({"id": "bad.b", "name": "B", "keywords": ["b"]}, lambda s, p: 1 / 0))
    rows = asyncio.run(reg.match_all(None))
    assert rows[0]["count"] == 1 and "ZeroDivisionError" in rows[1]["error"]


def test_experience_store_summary(tmp_path):
    exp = ExperienceStore(tmp_path / "x.jsonl")
    exp.add({"steps": [{"id": "a"}], "outcome": "previewed", "verify": "pass"})
    exp.add({"steps": [{"id": "a"}], "outcome": "previewed", "verify": "fail", "why": "重叠变多"})
    exp.add({"steps": [{"id": "a"}], "outcome": "committed", "verify": "pass"})
    assert exp.summary("a") == {"runs": 2, "passed": 1, "failed": 1, "committed": 1, "lastFailure": "重叠变多"}


def test_watchdog_retries_once_then_gives_up():
    calls = []

    async def slow():
        calls.append(1)
        await asyncio.sleep(0.2 if len(calls) == 1 else 0)
        return "ok"
    events = []
    assert asyncio.run(with_watchdog(slow, total=0.05, name="t", emit=events.append)) == "ok"
    assert len(calls) == 2 and events[0]["type"] == "recover"

    async def never():
        await asyncio.sleep(1)
    with pytest.raises(TimeoutError):
        asyncio.run(with_watchdog(never, total=0.02, name="t"))


def test_tier_agent_blocks_commit_of_failed_candidate(tmp_path):
    async def scan(s):
        return [{"id": "k.a", "count": 2, "targets": [{"reason": "两个"}]}]

    async def run(s, steps):
        return {"report": [{"id": "k.a", "status": "applied"}], "edits": {"x": 1}}

    async def verify(s, res, steps):
        return summarize_checks([{"id": "o", "name": "重叠", "status": "fail", "detail": "变多"}])
    exp = ExperienceStore(tmp_path / "k.jsonl")
    ag = TierAgent("k", caps=[{"id": "k.a", "keywords": ["挤"]}], experience=exp,
                   hooks={"scan": scan, "run": run, "verify": verify, "commit": lambda c: c.result})

    async def go():
        await ag.scan(None)
        assert [r["id"] for r in ag.route("太挤了")] == ["k.a"]
        cand = await ag.generate(None, [{"id": "k.a"}], goal="太挤了")
        with pytest.raises(ValueError):
            await ag.commit(cand)
        ag.discard(cand, "验证未通过")
    asyncio.run(go())
    rows = exp.read()
    assert [r["outcome"] for r in rows] == ["previewed", "discarded"] and rows[0]["verify"] == "fail"


# ------------------------------------------------------------------ 适配器
SPEC = {"assets": [{"id": "tree", "name": "柏树", "size_m": [2, 2, 6]}, {"id": "house", "name": "屋", "size_m": [6, 4, 5]},
                   {"id": "island", "name": "岛", "size_m": [40, 30, 8]}]}


def test_layout_rows_units_skip_and_roundtrip():
    layout = [{"id": "tree_1", "asset": "tree", "at": [0, 0, 5], "rot_z": 0, "scale": 1},
              {"id": "tree_2", "asset": "tree", "at": [1, 0, 5], "rot_z": 0, "scale": 1},
              {"id": "house_1", "asset": "house", "at": [10, 4, 5], "rot_z": 30, "scale": 2},
              {"id": "island_1", "asset": "island", "at": [0, 0, 0], "rot_z": 0, "scale": 1}]
    rows, unit = OBJ.layout_rows(layout, SPEC, {"tree_1": {"size": [2.4, 2.0, 6]}}, skip_assets={"island"})
    assert [r["id"] for r in rows] == ["tree_1", "tree_2", "house_1"]
    assert unit == pytest.approx(1.1 / 0.5)                    # 占地半径中位数 1.1 米 → 工坊 0.5
    assert rows[2]["r"] == pytest.approx((12 + 8) / 4 / unit)  # 房子 ×2
    edits = {"tree_2": {"dx": 0.5, "dz": -0.25, "dy": 0, "ry": 0.5, "s": 1.1}}
    out = OBJ.apply_edits(layout, edits, unit)
    t2 = next(x for x in out if x["id"] == "tree_2")
    assert t2["at"] == pytest.approx([1 + 0.5 * unit, -0.25 * unit, 5]) and t2["scale"] == pytest.approx(1.1) and t2["rot_z"] == pytest.approx(28.65, abs=0.01)
    assert next(x for x in out if x["id"] == "island_1") == layout[3]


def test_material_rows_and_fixes_roundtrip():
    mats = [{"root": "h1", "material": "osk_roof", "px": 300, "authored": [0.8, 0.4, 0.2]},
            {"root": "h2", "material": "osk_roof", "px": 200, "authored": [0.8, 0.4, 0.2]},
            {"root": "h1", "material": "osk_outline", "px": 50, "authored": [0, 0, 0]},
            {"root": "sea1", "material": "water", "px": 900, "authored": [0.1, 0.3, 0.5]}]
    roots = {"h1": "house", "h2": "house", "sea1": "sea"}
    rows = GS.material_rows(mats, roots, {"house": {"osk_roof": [1.2, 1.0, 1.0]}}, skip_assets={"sea"})
    assert [r["id"] for r in rows] == ["house/osk_roof"] and rows[0]["uses"] == 500 and rows[0]["base"] == "#cc6633"
    fixes = GS.colors_to_fixes(rows, {"house/osk_roof": rows[0]["color"]}, {})
    assert fixes["house"]["osk_roof"] == pytest.approx([1.2, 1.0, 1.0], abs=0.03)   # 当前色 → 同一套系数


def _hex_state():
    from genai_pipeline.oskar import program as OP
    prog = json.loads((ROOT / "projects/oskar-trials/programs/hex-island.ref.json").read_text())
    return OP.build_state(prog)


def test_oskar_state_to_world_and_back():
    st = _hex_state()
    w = CV.state_world(st)
    n = len(st.grid.verts)
    assert len(w["cells"]) == n and all(c["n"] == len(c["edgeNeighbors"]) for c in w["cells"])
    assert any(-1 in c["edgeNeighbors"] for c in w["cells"])           # 外边界 = 开阔海面
    rocks = sum(bin(m).count("1") for m in w["rock"])
    assert rocks == sum(1 for c in st.cells.values() if c.kind == "rock")
    # 加一根 3 层岩柱、删掉一格 → 写回角点状态
    empty = next(v for v in range(n) if w["solid"][v] == 0)
    full = next(v for v in range(n) if w["solid"][v] & 1)
    after = {k: list(v) if isinstance(v, list) else v for k, v in w.items()}
    after["solid"][empty] |= 0b111
    after["rock"][empty] |= 0b111
    top = w["solid"][full].bit_length() - 1
    after["solid"][full] &= ~(1 << top)
    after["rock"][full] &= ~(1 << top)
    n0 = len(st.cells)
    d = CV.apply_world(st, w, after)
    assert d == {"added": 3, "removed": 1} and len(st.cells) == n0 + 2
    assert st.cells[(empty, 2)].kind == "rock" and (full, top) not in st.cells


def test_voxel_apply_without_bridge_is_skipped(monkeypatch):
    monkeypatch.setattr(bridge, "available", lambda: (False, "找不到 Node"))
    assert CV.apply_to_state(_hex_state(), {"goal": "海中石柱"}) == {"status": "skipped", "why": "找不到 Node"}


# ------------------------------------------------------------------ 本管线能力的计划执行
def test_native_plan_runs_match_run_verify_and_reverts_failed(monkeypatch, tmp_path):
    from genai_pipeline import capabilities as CAPS, pipeline as P
    monkeypatch.setattr(CAPS, "experience", lambda tier: ExperienceStore(tmp_path / f"{tier}.jsonl"))

    async def fake_light(ctx, hub, spec, rec, target, cam):
        rec["light_rig"], rec["light_err"] = {"key": {}}, 0.9   # 误差很大 → 验证 fail → 撤回

    async def fake_sky(ctx, spec, rec, target, cam):
        p = tmp_path / "dome.png"
        p.write_bytes(b"x")
        rec["sky_dome"] = str(p)
    monkeypatch.setattr(P, "_light_loop", fake_light)
    monkeypatch.setattr(P, "_sky_dome", fake_sky)
    events = []
    ctx = SimpleNamespace(id="r1", opt=lambda k, d=None: d, emit=lambda kind, msg, data=None: events.append((kind, msg)))
    spec = {"palette_measured": {"regions": {"sky": "s.png", "terrain_rock": "r.png"}}}
    sub = SimpleNamespace(ctx=ctx, hub=None, spec=spec, rec={"rounds": [1]}, target="t.png", cam={"eye": [0, 0, 1]}, roots={})
    rep = asyncio.run(CAPS.run_native_plan(ctx, sub, ("env.sky-dome", "light.rig-fit", "camera.pitch-fit", "nope.x")))
    by = {r["id"]: r for r in rep}
    assert by["env.sky-dome"]["verify"] == "pass" and by["light.rig-fit"]["verify"] == "fail"
    assert "light_rig" not in sub.rec                                  # fail 的布光撤回
    assert by["camera.pitch-fit"]["status"] == "no-target"            # 已有组装轮次 → 不比俯角
    assert any("没有登记" in m for _, m in events)
    import json as _json
    _json.dumps([c.describe() for c in CAPS.NATIVE], ensure_ascii=False)      # 目录可 JSON 化
    sub2 = SimpleNamespace(ctx=ctx, hub=None, spec={"palette_measured": {"regions": {}}}, rec={}, target="t", cam={"eye": [0, 0, 1]}, roots={})
    asyncio.run(CAPS.run_native_plan(ctx, sub2, ("light.rig-fit",)))
    assert any("跳过" in m and "岩面" in m for _, m in events)
    rows = ExperienceStore(tmp_path / "scene3d.jsonl").read()
    assert [r["outcome"] for r in rows] == ["committed", "discarded"]


# ------------------------------------------------------------------ 真实的外部能力（TigerInBamboo）
@needs_external
def test_external_catalog_lists_all_tiers():
    d = asyncio.run(bridge.call("describe", {}))
    ids = {t: [c["id"] for c in d[t]] for t in ("voxel", "object", "gscene", "planet")}
    assert "terrain.arch" in ids["voxel"] and "object.spacing-relax" in ids["object"] and "scene.style-pack" in ids["gscene"]


@needs_external
def test_external_object_spacing_relax_on_our_layout():
    layout = [{"id": f"tree_{i}", "asset": "tree", "at": [i * 0.6, 0, 5], "rot_z": 0, "scale": 1} for i in range(6)]
    rows, unit = OBJ.layout_rows(layout, SPEC)
    scan = asyncio.run(bridge.call("object.scan", {"layout": rows}))
    assert next(m for m in scan if m["id"] == "object.spacing-relax")["count"] > 0
    meta = next(c for c in asyncio.run(bridge.call("describe", {}))["object"] if c["id"] == "object.spacing-relax")
    steps = [{"id": "object.spacing-relax", "params": params_for(meta)}]     # 验证按步骤参数算位移预算：参数要写全
    res = asyncio.run(bridge.call("object.run", {"layout": rows, "steps": steps}))
    ver = asyncio.run(bridge.call("object.verify", {"layout": rows, "edits": res["edits"], "steps": steps}))
    assert ver["status"] != "fail", ver
    out = OBJ.apply_edits(layout, res["edits"], unit)
    xs = [x["at"][0] for x in out]
    assert max(xs) - min(xs) > 3.0                                # 一排挤在一起的树被推开（原来跨 3 米）
    ov = next(c for c in ver["checks"] if c["id"] == "overlap")
    n0, n1 = [int(v) for v in ov["detail"].replace("对", "").split("→")]
    assert n1 < n0


@needs_external
def test_external_gscene_style_pack_routes_from_brief():
    rows = [{"id": f"a/m{i}", "name": f"m{i}", "base": c, "color": c, "uses": 100 + i}
            for i, c in enumerate(["#8a7a5a", "#2f4f3f", "#c04030", "#d8d0b8", "#3a5a8a"])]
    caps = asyncio.run(bridge.call("describe", {}))["gscene"]
    scan = asyncio.run(bridge.call("gscene.scan", {"materials": rows}))
    routed = route("莫里斯式中世纪挂毯：浓密纹样", caps, scan)
    assert [r["id"] for r in routed] == ["scene.style-pack"]
    res = asyncio.run(bridge.call("gscene.run", {"materials": rows, "steps": [{"id": "scene.style-pack"}]}))
    ver = asyncio.run(bridge.call("gscene.verify", {"materials": rows, "colors": res["colors"]}))
    assert res["colors"] and ver["status"] in ("pass", "warn")
    fixes = GS.colors_to_fixes(rows, res["colors"])
    assert set(fixes["a"]) == {r["id"].split("/")[1] for r in rows if r["id"] in res["colors"]}


@needs_external
def test_external_voxel_capabilities_on_oskar_state():
    st = _hex_state()
    n0 = len(st.cells)
    rep = CV.apply_to_state(st, {"goal": "海中立两根石柱，崖顶加悬垂"})
    assert rep["status"] in ("pass", "warn") and rep["applied"], rep
    assert set(rep["steps"]) == {"terrain.seastack", "terrain.overhang"}
    assert len(st.cells) == n0 + rep["diff"]["added"] - rep["diff"]["removed"]
    from genai_pipeline.oskar import program as OP
    assert all(c["passed"] for c in OP.check_state(st) if c["name"].startswith("无悬空"))
    assert CV.apply_to_state(_hex_state(), {"goal": "什么都不改"})["status"] == "no-route"
