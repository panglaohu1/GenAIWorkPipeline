"""Acceptance, account, registry, and learning integration without GPU workloads."""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from genai_pipeline.quality import assess
from genai_pipeline.console.auth import LocalAuth, COOKIE
from genai_pipeline import style_agents
from genai_pipeline.store import Store
from genai_pipeline.pipeline import Orchestrator, stage_deliver


def asset_state(score=8, hard=None):
    return {'spec':{'assets':[{'id':'a','name':'Asset'}]},
            'models':{'a':{'glb':'a.glb','best':0,'exported_round':1,
                'rounds':[{'round':1,'score':score,'review':{'pass':score>=7.5},'hard':hard or []}]}}}


def test_quality_never_equates_done_or_package_with_passing():
    state = asset_state(2.2, ['over budget'])
    state['final_review'] = {'review':{'score':4,'pass':False},'test':{'hard_ok':False,'facts':'4 runtime errors'}}
    q = assess({'mode':'game','status':'done'},state)
    assert q['status'] == 'blocked'
    assert q['failed_count'] == 4
    assert q['suggested_stage'] == 'assemble'


def test_quality_uses_exported_round_and_treats_missing_evidence_as_unknown():
    state = asset_state()
    assert assess({'mode':'asset'},state)['status'] == 'passed'
    state['models']['a']['rounds'].append({'round':2,'score':1,'review':{'pass':False},'hard':['bad']})
    state['models']['a']['best'] = 1
    assert assess({'mode':'asset'},state)['status'] == 'passed'
    state['models']['a']['exported_round'] = 3
    assert assess({'mode':'asset'},state)['status'] == 'unverified'


def test_oskar_legacy_export_location_remains_valid():
    checks=[{'name':'valid','required':True,'passed':True}]
    state={'oskar':{'checks':checks,'godot':{'ok':True,'checks':checks,'match':{'hero':.9}}},
           'models':{'oskar_scene':{'glb':'scene.glb'}}}
    assert assess({'mode':'oskar'},state)['status']=='passed'


def test_invalid_retry_preserves_options_and_stages(tmp_path):
    store = Store(tmp_path/'retry.sqlite')
    run_id = store.create_run('Test', 'Test', 'asset', {'model':'main'}, ['plan','model'])
    store.set_stage(run_id, 'plan', 'done')
    store.update_run(run_id, status='failed', error='model failed')
    before = store.get_run(run_id)
    stages = store.stages(run_id)
    orch = Orchestrator(store, start_worker=False)
    with pytest.raises(RuntimeError, match='没有阶段'):
        orch.retry(run_id, 'unknown', {'model':'fallback'})
    assert store.get_run(run_id) == before
    assert store.stages(run_id) == stages
    with pytest.raises(RuntimeError, match='任务不存在'):
        orch.retry('missing', options={'model':'fallback'})
    orch.retry(run_id, 'model', {'model':'fallback'})
    assert store.get_run(run_id)['options']['model'] == 'fallback'
    assert [s['status'] for s in store.stages(run_id)] == ['done', 'pending']


async def test_failed_delivery_writes_assessment_but_no_package(tmp_path):
    state=asset_state(2)
    ctx=SimpleNamespace(run={'mode':'asset'},options={},state=state,save=lambda:None,dir=tmp_path)
    with pytest.raises(RuntimeError,match='交付门禁'):
        await stage_deliver(ctx)
    assert state['quality']['status'] == 'blocked'
    assert not list(tmp_path.iterdir())


def test_password_hash_session_and_rate_limit(tmp_path):
    auth=LocalAuth(tmp_path/'auth.json')
    auth.setup('artist','a-test-password')
    assert 'a-test-password' not in auth.path.read_text()
    assert auth.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        auth.setup('another','another-password')
    token=auth.login('artist','a-test-password')
    assert auth.user(token)=='artist'
    assert auth.user('forged') is None
    auth.logout(token)
    assert auth.user(token) is None
    for _ in range(8):
        with pytest.raises(ValueError):
            auth.login('artist','wrong','client')
    with pytest.raises(ValueError,match='频繁'):
        auth.login('artist','a-test-password','client')


@pytest.fixture
def registry(monkeypatch,tmp_path):
    monkeypatch.setattr(style_agents,'_root',lambda:tmp_path/'agents')
    return tmp_path/'agents'


async def test_learning_is_draft_then_snapshot_is_pinned(registry,monkeypatch,tmp_path):
    from genai_pipeline.llm import LLM
    agent=style_agents.create('暮色群岛','黄昏世界',['先确定岛屿剪影。'])
    assert style_agents.snapshot(agent['id'])['version']=='baseline'
    old=style_agents.snapshot(agent['id'])
    async def fake_json(self,messages,**kwargs):
        obj={'summary':'提炼色彩规则','rules':[{'rule':'保留冷暖对比。','sources':['user:learning-brief']}]}
        assert kwargs['validate'](obj) is None
        assert kwargs['validate']({'rules':[{'rule':'bad','sources':['made-up']}]})
        return obj
    monkeypatch.setattr(LLM,'json',fake_json)
    run_dir=tmp_path/'run';run_dir.mkdir()
    ctx=SimpleNamespace(options={'style_agent':agent['id'],'style_profile':old},
                        run={'brief':'研究冷暖对比'},dir=run_dir,id='learn-1',state={},
                        emit=lambda *a:None,save=lambda:None,artifact=lambda *a:None)
    await style_agents.learn(ctx)
    assert style_agents.snapshot(agent['id'])==old
    profile=style_agents.profiles(agent['id'])[0]
    assert profile['source_ids']==['user:learning-brief']
    active=style_agents.activate(agent['id'],profile['version'])
    assert active['rules'][-1]=='保留冷暖对比。'
    assert old['rules']==['先确定岛屿剪影。']
    assert style_agents.activate(agent['id'],'baseline')==old


def test_submission_captures_agent_profile(registry,tmp_path):
    store=Store(tmp_path/'db.sqlite')
    orch=Orchestrator(store,start_worker=False)
    run_id=orch.submit('test','brief','asset',{'style_agent':'oskar'},start=False)
    run=store.get_run(run_id)
    assert run['options']['style_profile']['agent_id']=='oskar'
    assert run['options']['style_profile']['version']=='baseline'
    assert run['options']['style_profile']['rules']
    with pytest.raises(ValueError):
        orch.submit('test','brief','asset',{'style_agent':'missing'},start=False)


def test_inherited_knowledge_is_portable_sourced_and_pinned(registry,tmp_path,monkeypatch):
    import copy
    bundle=style_agents.knowledge('oskar')
    assert len(bundle['modules'])==13 and len(bundle['sources'])==12
    source_ids={s['id'] for s in bundle['sources']}
    for module in bundle['modules']:
        assert set(module['source_ids']) <= source_ids
        assert module['available'] and module['gap'] and module['validation']
    for source in bundle['sources']:
        assert style_agents.knowledge_source('oskar',source['id']).is_file()
    with pytest.raises(ValueError):
        style_agents.knowledge_source('oskar','../auth')
    store=Store(tmp_path/'pinned.db')
    orch=Orchestrator(store,start_worker=False)
    run_id=orch.submit('snapshot','brief','asset',{},start=False)
    pinned=store.get_run(run_id)['options']['style_profile']
    updated=copy.deepcopy(bundle)
    updated['version']='future'
    updated['modules'][0]['guidance']='future rule'
    monkeypatch.setattr(style_agents,'knowledge',lambda agent_id:updated)
    assert style_agents.snapshot('oskar')['knowledge']['version']=='future'
    assert store.get_run(run_id)['options']['style_profile']==pinned
    model_prompt=style_agents.rules_text(pinned,'model')
    assembly_prompt=style_agents.rules_text(pinned,'assemble')
    assert '未来' not in model_prompt and 'future rule' not in model_prompt
    assert '真实宽台面' in model_prompt
    assert '每顶点入向' not in model_prompt and '每顶点入向' in assembly_prompt
    assert '实际渲染' in style_agents.rules_text(pinned,'review')


async def test_inherited_modules_are_available_to_learning(registry,tmp_path,monkeypatch):
    from genai_pipeline.llm import LLM
    profile=style_agents.snapshot('oskar')
    async def learned(self,messages,**kwargs):
        sources=json.loads(messages[1]['content'])
        assert any(s['id']=='knowledge:shore' and '未移植' in s['text'] for s in sources)
        draft={'summary':'检验真实台面','rules':[{'rule':'先核查岩台真实面积。','sources':['knowledge:landform']}]}
        assert kwargs['validate'](draft) is None
        return draft
    monkeypatch.setattr(LLM,'json',learned)
    ctx=SimpleNamespace(options={'style_agent':'oskar','style_profile':profile},run={'brief':'复盘岩台'},
                        dir=tmp_path/'learning',id='inherit-test',state={},emit=lambda *a:None,
                        save=lambda:None,artifact=lambda *a:None)
    await style_agents.learn(ctx)
    saved=style_agents.profiles('oskar')[0]
    assert saved['knowledge_digest']==profile['knowledge']['digest']
    assert saved['rules'][0]['sources']==['knowledge:landform']
    assert style_agents.snapshot('oskar')['version']=='baseline'


async def test_console_auth_and_read_only_guard(tmp_path,monkeypatch,registry):
    monkeypatch.setenv('GENAI_CONSOLE_READ_ONLY','1')
    from genai_pipeline.console import app as web
    monkeypatch.setattr(web,'auth',LocalAuth(tmp_path/'account.json'))
    monkeypatch.setattr(web,'READ_ONLY',True)
    store=Store(tmp_path/'db.sqlite')
    monkeypatch.setattr(web,'store',store)
    monkeypatch.setattr(web,'orch',Orchestrator(store,start_worker=False))
    monkeypatch.setattr(web,'RUNS',tmp_path/'runs')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),base_url='http://test') as client:
        assert (await client.get('/api/runs')).status_code==401
        assert (await client.get('/api/agents/oskar/knowledge/tm-games')).status_code==401
        assert 'loginForm' in (await client.get('/')).text
        response=await client.post('/api/auth/setup',json={'username':'artist','password':'a-test-password'})
        assert response.status_code==200
        assert 'HttpOnly' in response.headers['set-cookie']
        assert (await client.get('/api/runs')).status_code==200
        assert 'agentsView' in (await client.get('/')).text
        assert (await client.get('/api/agents')).json()[0]['id']=='oskar'
        assert (await client.get('/api/agents/oskar/knowledge/tm-games')).status_code==200
        assert (await client.get('/api/agents/oskar/knowledge/missing')).status_code==404
        assert (await client.post('/api/agents',json={})).status_code==403
        assert (await client.post('/api/auth/logout',headers={'Origin':'https://elsewhere'})).status_code==403
        monkeypatch.setattr(web,'READ_ONLY',False)
        agent=(await client.post('/api/agents',json={'name':'Test','principles':['Simple shapes']})).json()
        imported=await client.post(f"/api/agents/{agent['id']}/sources",json={'title':'Shapes','text':'Use a clear silhouette.'})
        assert imported.status_code==200
        source=imported.json()
        assert (await client.get(f"/api/agents/{agent['id']}/sources/{source['id']}")).text=='Use a clear silhouette.'
        response=await client.post(f"/api/agents/{agent['id']}/learn",json={'brief':'Study shapes','runs':[], 'sources':[source['id']]})
        assert response.status_code==200
        run=store.get_run(response.json()['id'])
        assert run['mode']=='learn' and run['status']=='queued'
        assert run['options']['style_agent']==agent['id']
        assert run['options']['learning_sources'][0]['text']=='Use a clear silhouette.'
        archived=await client.post(f"/api/agents/{agent['id']}/sources/{source['id']}/archive",json={'archived':True})
        assert archived.status_code==200 and archived.json()['archived']
        assert (await client.post(f"/api/agents/{agent['id']}/learn",json={'sources':[source['id']]})).status_code==400
        monkeypatch.setattr(web,'READ_ONLY',True)
        assert (await client.post(f"/api/agents/{agent['id']}/sources",json={'title':'x','text':'y'})).status_code==403
        assert (await client.post(f"/api/agents/{agent['id']}/sources/{source['id']}/archive",json={'archived':False})).status_code==403
        assert (await client.post('/api/auth/logout')).status_code==200
        assert (await client.get('/api/agents')).status_code==401


def test_source_library_validation_archive_and_integrity(registry):
    agent=style_agents.create('角色顾问','角色与装备',['先看剪影'])['id']
    record=style_agents.import_source(agent,'角色研究','用大形组织装备。','https://example.org/research')
    assert 'text' not in record
    assert style_agents.import_source(agent,'角色研究','用大形组织装备。','https://example.org/research')['id']==record['id']
    assert style_agents.source(agent,record['id'])['text']=='用大形组织装备。'
    assert 'text' not in next(a for a in style_agents.catalog() if a['id']==agent)['sources'][0]
    for text,url in [('x'*8001,''),('',''),('a\x00b',''),('valid','javascript:alert(1)')]:
        with pytest.raises(ValueError): style_agents.import_source(agent,'bad',text,url)
    other=style_agents.create('其他顾问','',['规则'])['id']
    with pytest.raises(ValueError): style_agents.source(other,record['id'])
    with pytest.raises(ValueError): style_agents.source(agent,'../auth')
    style_agents.archive_source(agent,record['id'],True)
    with pytest.raises(ValueError,match='归档'): style_agents.learning_sources(agent,[record['id']])
    assert style_agents.source(agent,record['id'])['text']=='用大形组织装备。'
    style_agents.archive_source(agent,record['id'],False)
    assert len(style_agents.learning_sources(agent,[record['id'],record['id']]))==1
    path=registry/agent/'sources'/f"{record['id']}.json"
    tampered=json.loads(path.read_text());tampered['text']='changed';path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError,match='已改变'): style_agents.source(agent,record['id'])


async def test_learning_pins_selected_documents_before_queue(registry,tmp_path,monkeypatch):
    from genai_pipeline.llm import LLM
    agent=style_agents.create('风格顾问','',['规则'])['id']
    old=style_agents.import_source(agent,'访谈笔记','留出安静区，让角色动作成为视觉重点。')
    store=Store(tmp_path/'sources.sqlite');orch=Orchestrator(store,start_worker=False)
    run_id=orch.submit('学习','研究节奏','learn',{'style_agent':agent,'learning_source_ids':[old['id']]},start=False)
    options=store.get_run(run_id)['options']
    assert options['learning_sources'][0]['sha256']==old['sha256']
    style_agents.archive_source(agent,old['id'],True)
    style_agents.import_source(agent,'新版笔记','这是后续的新内容')
    async def fake_json(self,messages,**kwargs):
        evidence=json.loads(messages[1]['content'])
        imported=[s for s in evidence if s['id'].startswith('document:')]
        assert len(imported)==1 and imported[0]['text']=='留出安静区，让角色动作成为视觉重点。'
        assert '后续的新内容' not in messages[1]['content']
        draft={'summary':'安静区与动作对比','rules':[{'rule':'为关键动作留出安静区。','sources':[imported[0]['id']]}]}
        assert kwargs['validate'](draft) is None
        return draft
    monkeypatch.setattr(LLM,'json',fake_json)
    ctx=SimpleNamespace(options=options,run={'brief':'研究节奏'},dir=tmp_path/'learn',id=run_id,state={},
                        emit=lambda *a:None,save=lambda:None,artifact=lambda *a:None)
    await style_agents.learn(ctx)
    profile=style_agents.profiles(agent)[0]
    assert profile['sources'][0]['title']=='访谈笔记'
    assert profile['rules'][0]['sources']==['document:'+old['id']]
    assert style_agents.snapshot(agent)['version']=='baseline'
    with pytest.raises(ValueError): orch.submit('bad','brief','learn',{'style_agent':agent,'learning_source_ids':['missing']},start=False)
    assert len(store.list_runs())==1
