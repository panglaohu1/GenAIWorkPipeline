"""游戏组装 · 星球：已验收的 Oskar 场景按程序重建 → 地势连接 → 贴球 → 星球游戏（替身工具，不跑 Blender / Godot / LLM）。"""
import json
import struct
import uuid

import pytest

from genai_pipeline import assembly, config, planet_assembly, style_agents
from genai_pipeline.agent import RunControl
from genai_pipeline.godot import oskar_scene as OG
from genai_pipeline.oskar import director as D
from genai_pipeline.pipeline import Orchestrator, RunContext
from genai_pipeline.quality import assess
from genai_pipeline.store import Store

PROGRAM = {"grid": {"radius": 6, "cell": 2.0}, "level_h": 2.0, "sea_level": 1, "seed": 3,
           "items": [{"type": "mass", "kind": "rock", "at": [0, 0], "size": [1.2, 1.0], "top": 4}]}


def glb():
    payload = b'{"asset":{"version":"2.0"}}  '
    return b'glTF' + struct.pack('<II', 2, 20 + len(payload)) + struct.pack('<I', len(payload)) + b'JSON' + payload


class Hub:
    def __init__(self, d):
        self.dir, self.calls = d, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def call(self, name, args):
        from PIL import Image
        self.calls.append((name, args))
        if name == "blender_build_oskar":
            walk = [[x, y, 4.0] for x in range(-14, 15, 2) for y in range(-14, 15, 2)]
            return {"ok": True, "walk": walk, "paths": [[[0, -12, 2.0], [0, 0, 4.0]]], "size": [40, 40, 10]}
        if name == "blender_export_glb":
            p = self.dir / "blender" / "export" / f"{args['asset_name']}.glb"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(glb())
            return {"ok": True, "path": str(p), "info": {"size": [40, 40, 10], "triangles": 10}}
        if name == "blender_compose_planet":
            assert args["links"] == [] and sum(i.get("kind") == "link" for i in args["items"]) == 2
            return {"ok": True, "errors": [], "sizes": {}}
        if name == "blender_render":
            p = self.dir / "renders" / f"{args['label']}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (8, 8)).save(p)
            return {"images": [str(p)]}
        raise AssertionError(name)


@pytest.mark.asyncio
async def test_planet_assembly_rebuilds_scenes_links_them_by_terrain_and_builds_the_game(tmp_path, monkeypatch):
    monkeypatch.setitem(config.load()['pipeline'], 'runs_dir', str(tmp_path / 'runs'))
    monkeypatch.setattr(style_agents, '_root', lambda: tmp_path / 'agents')
    store = Store(tmp_path / 'store.sqlite')
    orch = Orchestrator(store, start_worker=False)
    ids = []
    for title in ('十二门徒', '高山圣城', '新城'):
        rid = store.create_run(title, 'source', 'oskar', {}, ['oskar_build'])
        ids.append(rid)
        d = assembly.root() / rid
        d.mkdir(parents=True)
        (d / 'oskar_scene.glb').write_bytes(glb())
        (d / 'state.json').write_text(json.dumps({
            'spec': {'assets': [{'id': 'oskar_scene', 'name': title}]},
            'models': {'oskar_scene': {'glb': str(d / 'oskar_scene.glb')}},
            'oskar': {'program': PROGRAM, 'checks': [{'name': '结构', 'required': True, 'passed': True}],
                      'godot': {'ok': True, 'checks': [{'name': '运行', 'required': True, 'passed': True}], 'match': {'hero': 0.9}}}}))
        store.update_run(rid, status='done')
    sel = [{'id': rid, 'role': 'level', 'goal': ''} for rid in ids]
    with pytest.raises(ValueError):
        orch.compose_game('星球', '收集信件', sel[:1], 'oskar', str(uuid.uuid4()), 'planet')
    run_id = orch.compose_game('高山圣城星球', '小老虎收集每站的信', sel, 'oskar', str(uuid.uuid4()), 'planet')
    run = store.get_run(run_id)
    assert [s['name'] for s in store.stages(run_id)][:3] == ['assembly_plan', 'planet_compose', 'planet_godot']
    assert all(p.get('program') == PROGRAM for p in run['options']['assembly']['parts'])
    ctx = RunContext(store, run, RunControl())
    hubs = []
    monkeypatch.setattr(planet_assembly, 'MCPHub', lambda d, **kw: hubs.append(Hub(d)) or hubs[-1])

    async def plan_links(specs, **kw):
        return D.normalize_link_plan({'links': [{'id': specs[0]['id'], 'kind': 'ridge', 'why': '两端都高'}]}, specs)
    monkeypatch.setattr(D, 'plan_links', plan_links)
    seen = {}

    async def setup_and_verify(hub, proj, **kw):
        seen.update(kw)
        proj.mkdir(parents=True, exist_ok=True)
        (proj / 'project.godot').write_text('')
        (proj / 'assets').mkdir(exist_ok=True)
        (proj / 'assets' / 'oskar_atlas.glb').write_bytes(glb())
        return {'ok': True, 'checks': [{'name': '运行', 'required': True, 'passed': True, 'detail': ''}], 'shots': [],
                'labels': [], 'match': {}, 'fps': 60, 'project': str(proj)}
    monkeypatch.setattr(OG, 'setup_and_verify', setup_and_verify)
    for stage in ('assembly_plan', 'planet_compose', 'planet_godot'):
        ctx.stage = stage
        await {'assembly_plan': assembly.plan, 'planet_compose': planet_assembly.compose,
               'planet_godot': planet_assembly.godot}[stage](ctx)
    pl = ctx.state['planet']
    builds = [a for n, a in hubs[0].calls if n == 'blender_build_oskar']
    assert len(builds) == 3 + 2                                        # 三站按程序重建 + 两段地势连接
    assert [t['kind'] for t in pl['terrain_links']][0] == 'ridge' and len(pl['terrain_links']) == 2
    assert len(pl['game']['letters']) == 3 and len(pl['game']['tram']['segments']) == 3
    assert seen['main_gd'] == planet_assembly.PLANET_MAIN_GD and 'game' in seen['extra']
    assert len(seen['extra']['game']['letters']) == 3
    q = assess({**ctx.run, 'options': ctx.options}, ctx.state)
    assert [c['stage'] for c in q['issues']] == ['journey']           # 只剩人工试玩确认
    assembly.validate_delivery(ctx)
