"""Consultation queue and evidence isolation. All model replies are scripted."""
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from genai_pipeline import config, consultation, style_agents
from genai_pipeline.llm import LLM
from genai_pipeline.pipeline import Orchestrator
from genai_pipeline.store import Store
from genai_pipeline.quality import assess


@pytest.fixture
def studio(tmp_path,monkeypatch):
    monkeypatch.setitem(config.load()['pipeline'],'runs_dir',str(tmp_path/'runs'))
    monkeypatch.setattr(style_agents,'_root',lambda:tmp_path/'agents')
    store=Store(tmp_path/'db.sqlite');orch=Orchestrator(store,start_worker=False)
    aid=style_agents.create('试验顾问','视觉与空间',['优先辨认剪影。'])['id']
    parent=store.create_run('原作','保留角色身份','asset',{},['plan','model'])
    store.update_run(parent,status='done')
    directory=tmp_path/'runs'/parent;directory.mkdir(parents=True)
    (directory/'state.json').write_text(json.dumps({'spec':{'title':'原作','assets':[{'id':'hero','name':'角色'}]}}))
    Image.new('RGB',(8,8),(90,120,150)).save(directory/'render.png')
    store.add_artifact(parent,'model','render',directory/'render.png','角色测试图')
    return store,orch,aid,parent,directory


def reply():
    return {'summary':'保持角色身份，改善轮廓可读性。','changes':[{'area':'visual','title':'减少轮廓噪声',
        'reason':'原作需求要求保留角色身份。','change':'先比较主轮廓，再调整非必要的小饰件。',
        'acceptance':'以相同机位对照渲染，人工确认角色仍可辨认。','sources':['parent:brief','rule:1','image:1']}]}


async def test_invitation_freezes_parent_images_and_agent_without_editing_original(studio,monkeypatch):
    store,orch,aid,parent,directory=studio
    before=store.get_run(parent)
    task=orch.invite(parent,aid,'改善视觉',['visual'],'保留身份')
    assert store.get_run(parent)==before
    run=store.get_run(task);frozen=run['options']['consultation'];image=Path(frozen['images'][0]['path'])
    assert run['status']=='queued' and image.parent.parent.name==task
    assert image.read_bytes()==(directory/'render.png').read_bytes()
    original_digest=frozen['state_digest']
    # Original edits after invitation cannot replace evidence already queued.
    (directory/'state.json').write_text('{"spec":{"title":"later"}}')
    Image.new('RGB',(8,8),(255,0,0)).save(directory/'render.png')
    original_after=(directory/'state.json').read_bytes()
    async def fake(self,messages,**kwargs):
        assert isinstance(messages[1]['content'],list)
        assert any(x['type']=='image_url' for x in messages[1]['content'])
        assert kwargs['validate'](reply()) is None
        bad=reply();bad['changes'][0]['area']='gameplay';assert kwargs['validate'](bad)
        bad=reply();bad['changes'][0]['sources']=['imagined'];assert kwargs['validate'](bad)
        return reply()
    monkeypatch.setattr(LLM,'json',fake)
    await orch._execute(run)
    state=json.loads((image.parent.parent/'state.json').read_text())
    assert store.get_run(task)['status']=='done'
    assert state['consultation']['changes'][0]['execution']=='proposal_only'
    assert state['consultation']['parent_digest']==frozen['digest']
    assert frozen['state_digest']==original_digest
    assert (directory/'state.json').read_bytes()==original_after
    assert store.get_run(parent)==before
    assert assess(store.get_run(task),state)['label']=='提案待审核'
    assert not list(image.parent.parent.glob('*deliverables*'))


async def test_missing_or_modified_evidence_blocks_consultation(studio):
    store,orch,aid,parent,directory=studio
    task=orch.invite(parent,aid,'审阅',['space'])
    run=store.get_run(task)
    Path(run['options']['consultation']['images'][0]['path']).write_bytes(b'changed')
    await orch._execute(run)
    assert store.get_run(task)['status']=='failed'
    assert '图像快照缺失或改变' in store.get_run(task)['error']


def test_invitation_rejects_active_tasks_and_invalid_scopes(studio):
    store,orch,aid,parent,directory=studio
    for areas in ([],['unbounded'],[{}]):
        with pytest.raises(ValueError):orch.invite(parent,aid,'review',areas)
    store.update_run(parent,status='running')
    with pytest.raises(ValueError,match='正在执行'):orch.invite(parent,aid,'review',['visual'])
    assert len(store.list_runs())==1


def test_run_creation_rolls_back_incomplete_stages(tmp_path):
    store=Store(tmp_path/'atomic.sqlite')
    with pytest.raises(Exception):store.create_run('broken','brief','asset',{},['plan','plan'])
    assert store.list_runs()==[]
    assert store._conn().execute('select count(*) from stages').fetchone()[0]==0
    run=store.create_run('ready','brief','asset',{},['plan','model'],status='preparing')
    assert store.get_run(run)['status']=='preparing'
    assert len(store.stages(run))==2


async def test_invite_api_guards_and_queued_response(studio,tmp_path,monkeypatch):
    monkeypatch.setenv('GENAI_CONSOLE_READ_ONLY','1')
    from genai_pipeline.console import app as web
    from genai_pipeline.console.auth import LocalAuth
    store,orch,aid,parent,directory=studio
    monkeypatch.setattr(web,'auth',LocalAuth(tmp_path/'auth.json'))
    monkeypatch.setattr(web,'store',store);monkeypatch.setattr(web,'orch',orch)
    monkeypatch.setattr(web,'READ_ONLY',True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),base_url='http://test') as client:
        data={'agent_id':aid,'brief':'review','areas':['visual'],'preserve':'identity'}
        assert (await client.post(f'/api/runs/{parent}/consult',json=data)).status_code==401
        await client.post('/api/auth/setup',json={'username':'artist','password':'test-password'})
        assert (await client.post(f'/api/runs/{parent}/consult',json=data)).status_code==403
        monkeypatch.setattr(web,'READ_ONLY',False)
        response=await client.post(f'/api/runs/{parent}/consult',json=data)
        assert response.status_code==200
        assert store.get_run(response.json()['id'])['options']['parent_run']==parent
        assert (await client.post('/api/runs',data={'brief':'x','mode':'consult','options':'{}'})).status_code==400


async def prepared_revision(studio,monkeypatch):
    store,orch,aid,parent,directory=studio
    model=directory/'original.glb';model.write_bytes(b'glTF-test-snapshot')
    state=json.loads((directory/'state.json').read_text());state['models']={'hero':{'glb':str(model)}}
    (directory/'state.json').write_text(json.dumps(state))
    task=orch.invite(parent,aid,'改善剪影',['visual'],'保留身份')
    async def fake(self,messages,**kwargs):return reply()
    monkeypatch.setattr(LLM,'json',fake)
    await orch._execute(store.get_run(task))
    proposal=json.loads((directory.parent/task/'state.json').read_text())['consultation']
    return task,proposal


async def test_revision_is_independent_pinned_and_idempotent(studio,monkeypatch):
    store,orch,aid,parent,directory=studio
    task,proposal=await prepared_revision(studio,monkeypatch)
    pinned=store.get_run(task)['options']['style_profile']
    # Later changes to original files and the registry must not alter the approved revision.
    (directory/'original.glb').write_bytes(b'changed original')
    definition=style_agents.get(aid);definition['principles']=['later style']
    style_agents._save(style_agents._root()/'registry'/f'{aid}.json',definition)
    new_id=orch.revise(task,proposal['version'],['change-1'])
    assert orch.revise(task,proposal['version'],['change-1'])==new_id
    new=store.get_run(new_id);options=new['options']
    assert new['mode']=='asset' and new['status']=='queued'
    assert options['style_profile']==pinned
    assert options['approval_gates']==['concept']
    assert options['revision']['consultation_id']==task
    assert Path(options['source_model']).read_bytes()==b'glTF-test-snapshot'
    assert Path(options['source_model']).is_relative_to(directory.parent/new_id)
    assert (directory/'original.glb').read_bytes()==b'changed original'
    assert not (directory.parent/new_id/'state.json').exists()
    assert all(s['status']=='pending' for s in store.stages(new_id))
    assert assess(new,{})['status']=='unverified'
    assert '减少轮廓噪声' in new['brief'] and '保留身份' in new['brief']
    assert len(store.list_runs())==3


async def test_revision_rejects_stale_or_unsupported_selection(studio,monkeypatch):
    store,orch,aid,parent,directory=studio
    task,proposal=await prepared_revision(studio,monkeypatch)
    for version,selected in [('stale',['change-1']),(proposal['version'],[]),(proposal['version'],['made-up'])]:
        with pytest.raises(ValueError):orch.revise(task,version,selected)
    path=directory.parent/task/'state.json';state=json.loads(path.read_text())
    state['consultation']['changes'][0]['area']='gameplay';path.write_text(json.dumps(state))
    with pytest.raises(ValueError,match='支持视觉与角色'):orch.revise(task,proposal['version'],['change-1'])
    assert len(store.list_runs())==2


async def test_concurrent_revision_requests_create_one_child(studio,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    store,orch,aid,parent,directory=studio
    task,proposal=await prepared_revision(studio,monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(orch.revise,task,proposal['version'],['change-1']) for _ in range(2)]
        assert len({f.result() for f in futures})==1
    assert len(store.list_runs())==3


async def test_comparison_keeps_baseline_after_original_and_consultation_removed(studio, monkeypatch):
    store,orch,aid,parent,directory=studio
    task,proposal=await prepared_revision(studio,monkeypatch)
    from genai_pipeline import consultation
    child=orch.revise(task,proposal['version'],['change-1'])
    run=store.get_run(child)
    before=consultation.comparison(store,run,{})
    baseline=before['baseline']
    frozen=Path(baseline['images'][0]['path'])
    assert frozen.parent.parent.name==child
    assert frozen.read_bytes()==(directory/'render.png').read_bytes()
    assert before['current']['quality']['status']=='unverified'
    assert before['current']['images']==[]
    store.delete_run(parent);store.delete_run(task)
    (directory/'state.json').write_text('{}')
    assert consultation.comparison(store,run,{})['baseline']==baseline
    assert frozen.is_file()
    # Concept images and paths outside this revision are not presented as new renders.
    store.add_artifact(child,'concept','concept',directory/'render.png','not a render')
    store.add_artifact(child,'model','render',directory/'render.png','outside revision')
    target=frozen.parent.parent/'render.png';target.write_bytes(frozen.read_bytes())
    store.add_artifact(child,'model','render',target,'改版渲染样本')
    result=consultation.comparison(store,run,{})
    assert [i['label'] for i in result['current']['images']]==['改版渲染样本']
    assert '不自动判定' in result['notice']


async def test_revision_rejects_changed_baseline_image(studio,monkeypatch):
    store,orch,aid,parent,directory=studio
    task,proposal=await prepared_revision(studio,monkeypatch)
    evidence=store.get_run(task)['options']['consultation']
    Path(evidence['images'][0]['path']).write_bytes(b'changed')
    with pytest.raises(ValueError,match='图像快照已改变'):
        orch.revise(task,proposal['version'],['change-1'])
    children=[r for r in store.list_runs() if r['options'].get('revision')]
    assert len(children)==1 and children[0]['status']=='failed'
    assert orch._next() is None


@pytest.mark.parametrize('damage',['missing_option','missing_file','changed_file'])
async def test_damaged_revision_never_falls_back_to_new_creation(studio,monkeypatch,damage):
    from genai_pipeline import pipeline
    store,orch,aid,parent,directory=studio
    task,proposal=await prepared_revision(studio,monkeypatch)
    child=orch.revise(task,proposal['version'],['change-1'])
    run=store.get_run(child);source=Path(run['options']['source_model'])
    if damage=='missing_option':
        options=run['options'];options.pop('source_model')
        store.update_run(child,options=options)
    elif damage=='missing_file':source.unlink()
    else:source.write_bytes(b'changed after queue')
    async def forbidden(ctx):
        raise AssertionError('must not start generation with damaged revision input')
    monkeypatch.setitem(pipeline.STAGE_FUNCS,'plan',forbidden)
    await orch._execute(store.get_run(child))
    failed=store.get_run(child)
    assert failed['status']=='failed' and '原模型快照' in failed['error']
    assert all(s['status']=='pending' for s in store.stages(child))
    assert store.events(child)[-1]['kind']=='error'
    with pytest.raises(RuntimeError,match='原模型快照'):
        orch.retry(child)
    assert store.get_run(child)==failed
