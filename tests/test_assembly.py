"""Composition contract tests: isolated store, tiny GLBs, no inference or GPU jobs."""
import asyncio
import json
import struct
import uuid
from pathlib import Path

import httpx
import pytest

from genai_pipeline import config, assembly, style_agents
from genai_pipeline.agent import RunControl
from genai_pipeline.pipeline import Orchestrator, RunContext, stage_deliver
from genai_pipeline.quality import assess
from genai_pipeline.store import Store


def glb():
    payload=b'{"asset":{"version":"2.0"}}  '
    return b'glTF'+struct.pack('<II',2,20+len(payload))+struct.pack('<I',len(payload))+b'JSON'+payload


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setitem(config.load()['pipeline'],'runs_dir',str(tmp_path/'runs'))
    monkeypatch.setattr(style_agents,'_root',lambda:tmp_path/'agents')
    agent=style_agents.create('组装顾问','空间与关卡',['保持空间清晰'])['id']
    store=Store(tmp_path/'store.sqlite');orch=Orchestrator(store,start_worker=False)
    ids=[]
    for title in ('湖沼','圣城'):
        rid=store.create_run(title,'source','oskar',{},['oskar_build']);ids.append(rid)
        directory=assembly.root()/rid;directory.mkdir(parents=True)
        path=directory/'oskar_scene.glb';path.write_bytes(glb())
        state={'spec':{'assets':[{'id':'oskar_scene','name':title}]},
               'models':{'oskar_scene':{'glb':str(path)}},
               'oskar':{'checks':[{'name':'结构','required':True,'passed':True}],
                        'godot':{'ok':True,'checks':[{'name':'运行','required':True,'passed':True}],'match':{'score':1}}}}
        (directory/'state.json').write_text(json.dumps(state))
        store.update_run(rid,status='done')
    return store,orch,agent,ids


def create(setup,**kw):
    store,orch,agent,ids=setup
    return orch.compose_game('湖沼到圣城','找到出口并依次通过关卡',
        kw.get('selections',[{'id':rid,'role':'level','goal':'找到出口'} for rid in ids]),agent,kw.get('key',str(uuid.uuid4())))


def test_catalog_requires_acceptance_and_valid_files(setup):
    store,orch,agent,ids=setup
    assert len(assembly.catalog(store)['items'])==2
    store.update_run(ids[0],status='running')
    assert len(assembly.catalog(store)['items'])==1
    store.update_run(ids[0],status='done')
    path=assembly.root()/ids[0]/'state.json';state=json.loads(path.read_text())
    state['oskar']['godot']['ok']=False;path.write_text(json.dumps(state))
    assert len(assembly.catalog(store)['items'])==1
    (assembly.root()/ids[1]/'oskar_scene.glb').unlink()
    assert assembly.catalog(store)['items']==[]
    with pytest.raises((ValueError,OSError)):create(setup)
    assert len(store.list_runs())==2


@pytest.mark.parametrize('selections',[[],[{'id':'absent','role':'level'}],[{'id':{},'role':'level'}]])
def test_invalid_selection_is_rejected_before_creating_run(setup,selections):
    with pytest.raises((ValueError,OSError)):create(setup,selections=selections)
    assert len(setup[0].list_runs())==2


def test_roles_duplicates_and_required_level(setup):
    ids=setup[3]
    for parts in ([{'id':r,'role':'prop'} for r in ids],
                  [{'id':ids[0],'role':'level'}]*2,
                  [{'id':ids[0],'role':'wrong'}],
                  [{'id':r,'role':'player'} for r in ids]):
        with pytest.raises(ValueError):create(setup,selections=parts)


async def test_freezes_order_namespaces_sources_and_agent_with_idempotency(setup):
    store,orch,agent,ids=setup;before=[store.get_run(r) for r in ids];key=str(uuid.uuid4())
    task=create(setup,selections=[{'id':r,'role':'level'} for r in reversed(ids)],key=key)
    assert create(setup,key=key)==task
    run=store.get_run(task);m=run['options']['assembly']
    assert run['status']=='queued' and [p['source_id'] for p in m['parts']]==list(reversed(ids))
    assert [p['assets'][0]['id'] for p in m['parts']]==['part_01_01','part_02_01']
    assert run['options']['style_profile']['agent_id']==agent
    assert [store.get_run(r) for r in ids]==before
    # A later edit/deletion in a parent does not change the pinned game inputs.
    (assembly.root()/ids[0]/'oskar_scene.glb').unlink()
    ctx=RunContext(store,run,RunControl());ctx.stage='assembly_plan';await assembly.plan(ctx)
    assert len(ctx.state['spec']['assets'])==2
    assert ctx.state['spec']['scene']['ordered_levels'][0]['title']=='圣城'
    assert all(Path(m['glb']).is_file() for m in ctx.state['models'].values())
    assert assess(run,ctx.state)['status']!='passed'


async def test_tampered_inputs_fail_before_worker_can_execute_stages(setup):
    store,orch,agent,ids=setup;task=create(setup);run=store.get_run(task)
    path=Path(run['options']['assembly']['parts'][0]['assets'][0]['glb']);path.write_bytes(b'broken')
    await orch._execute(run)
    assert store.get_run(task)['status']=='failed'
    assert not (assembly.root()/task/'godot_project').exists()
    with pytest.raises(ValueError):orch.retry(task)


async def test_delivery_needs_new_game_checks_and_human_journey(setup):
    store,orch,agent,ids=setup;task=create(setup);run=store.get_run(task)
    ctx=RunContext(store,run,RunControl());ctx.stage='assembly_plan';await assembly.plan(ctx)
    ctx.state['final_review']={'test':{'hard_ok':True},'review':{'score':8,'pass':True}}
    q=assess(run,ctx.state)
    assert q['status']=='unverified' and q['issues'][0]['stage']=='journey'
    ctx.stage='journey';ctx.control.approvals['journey']={'selections':{'journey_played':True}}
    await assembly.journey(ctx)
    assert assess(run,ctx.state)['status']=='passed'
    ctx.state['final_review']['test']['hard_ok']=False
    assert assess(run,ctx.state)['status']=='blocked'
    ctx.state['final_review']['test']['hard_ok']=True
    with pytest.raises(ValueError,match='缺少游戏工程'):await stage_deliver(ctx)
    project=ctx.dir/'godot_project';(project/'assets').mkdir(parents=True)
    (project/'project.godot').write_text('[application]\nconfig/name="Test"')
    for model in ctx.state['models'].values():
        p=Path(model['glb']);(project/'assets'/p.name).write_bytes(p.read_bytes())
    ctx.stage='deliver';await stage_deliver(ctx)
    assert (ctx.dir/'deliverables'/'godot_project'/'project.godot').is_file()
    assert (ctx.dir/'deliverables'/'assembly-manifest.json').is_file()
    next((project/'assets').glob('*.glb')).write_bytes(b'changed')
    with pytest.raises(ValueError,match='固定版本不一致'):await stage_deliver(ctx)


async def test_api_auth_readonly_creation_and_journey_gate(setup,tmp_path,monkeypatch):
    monkeypatch.setenv('GENAI_CONSOLE_READ_ONLY','1')
    from genai_pipeline.console import app as web
    from genai_pipeline.console.auth import LocalAuth
    store,orch,agent,ids=setup
    monkeypatch.setattr(web,'store',store);monkeypatch.setattr(web,'orch',orch)
    monkeypatch.setattr(web,'auth',LocalAuth(tmp_path/'auth.json'));monkeypatch.setattr(web,'READ_ONLY',True)
    body={'title':'游戏','brief':'旅程','selections':[{'id':r,'role':'level'} for r in ids],
          'agent_id':agent,'request_id':str(uuid.uuid4())}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),base_url='http://test') as client:
        assert (await client.get('/api/assembly/catalog')).status_code==401
        await client.post('/api/auth/setup',json={'username':'artist','password':'test-password'})
        assert len((await client.get('/api/assembly/catalog')).json()['items'])==2
        assert (await client.post('/api/assembly',json=body)).status_code==403
        monkeypatch.setattr(web,'READ_ONLY',False)
        assert (await client.post('/api/runs',data={'brief':'bad','mode':'assembly'})).status_code==400
        response=await client.post('/api/assembly',json=body);assert response.status_code==200
        rid=response.json()['id'];assert store.get_run(rid)['status']=='queued'
        assert (await client.post('/api/assembly',json=body)).json()['id']==rid
        assert (await client.post(f'/api/runs/{rid}/approve',json={'gate':'journey','selections':{'journey_played':True}})).status_code==409
        store.update_run(rid,status='waiting',current_stage='journey')
        assert (await client.post(f'/api/runs/{rid}/approve',json={'gate':'journey'})).status_code==400
        assert (await client.post(f'/api/runs/{rid}/approve',json={'gate':'journey','selections':{'journey_played':True}})).status_code==200


async def test_entire_queue_executes_integration_stages_and_stops_for_human(setup,monkeypatch):
    from genai_pipeline.pipeline import STAGE_FUNCS
    store,orch,agent,ids=setup;task=create(setup)
    async def fake_assemble(ctx):
        project=ctx.dir/'godot_project';(project/'assets').mkdir(parents=True)
        (project/'project.godot').write_text('[application]\nconfig/name="Scripted test"')
        for model in ctx.state['models'].values():
            p=Path(model['glb']);(project/'assets'/p.name).write_bytes(p.read_bytes())
    async def fake_review(ctx):
        ctx.state['final_review']={'test':{'hard_ok':True},'review':{'score':8,'pass':True}}
        ctx.save()
    monkeypatch.setitem(STAGE_FUNCS,'assemble',fake_assemble)
    monkeypatch.setitem(STAGE_FUNCS,'review',fake_review)
    pending=asyncio.create_task(orch._execute(store.get_run(task)))
    try:
        for _ in range(100):
            if store.get_run(task)['status']=='waiting':break
            await asyncio.sleep(.01)
        run=store.get_run(task)
        assert run['status']=='waiting' and run['current_stage']=='journey'
        assert not list((assembly.root()/task).glob('*deliverables.zip'))
        orch.control(task).approvals['journey']={'selections':{'journey_played':True}}
        await asyncio.wait_for(pending,2)
        assert store.get_run(task)['status']=='done'
        assert all(s['status']=='done' for s in store.stages(task))
        assert list((assembly.root()/task).glob('*deliverables.zip'))
    finally:
        if not pending.done():pending.cancel()


async def test_failed_automatic_checks_do_not_request_human_signoff(setup):
    store,orch,agent,ids=setup;task=create(setup)
    ctx=RunContext(store,store.get_run(task),RunControl());ctx.stage='assembly_plan';await assembly.plan(ctx)
    ctx.state['final_review']={'test':{'hard_ok':False},'review':{'score':8,'pass':True}}
    with pytest.raises(RuntimeError,match='自动验收尚未通过'):await assembly.journey(ctx)
    assert ctx.state['assembly']['journey_approved'] is False
    assert store.get_run(task)['status']!='waiting'


def test_retry_from_journey_restarts_at_failing_upstream_stage(setup):
    store,orch,agent,ids=setup
    rid=create(setup)
    d=assembly.root()/rid
    state={'final_review':{'test':{'hard_ok':False,'facts':'脚本错误'},'review':{'score':1.0,'pass':False}},
           'assembly':{'sources_verified':True,'journey_approved':False}}
    (d/'state.json').write_text(json.dumps(state))
    for s in ('assembly_plan','assemble','review'):
        store.set_stage(rid,s,'done')
    store.set_stage(rid,'journey','failed')
    store.update_run(rid,status='failed')
    orch.retry(rid,'journey')
    assert orch.start_from[rid]=='assemble'
    assert [s['status'] for s in store.stages(rid)][1:4]==['pending','pending','pending']
