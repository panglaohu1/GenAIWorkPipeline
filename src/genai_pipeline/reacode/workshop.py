"""Learning provenance and revision-bound candidate execution, all on local code."""
from __future__ import annotations
import hashlib
import json
import time
import uuid
from pathlib import Path
from .. import config
from ..capabilities import bridge
from ..capabilities.contract import ExperienceStore

ROOT = config.resolve('engines/REACodeRefactoring')
DATA = config.resolve('data/reacode')

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def learning():
    path=ROOT/'learn/pipeline.js'
    text=path.read_text()
    payload=json.JSONDecoder().raw_decode(text[text.index('{'):])[0]
    return {'engine':'REACodeRefactoring','compute':'local','pipeline':payload,
            'links':{'learning':'/engines/REACodeRefactoring/learn.html',
                     'architecture':'/engines/REACodeRefactoring/architecture.html',
                     'engine':'/engines/REACodeRefactoring/index.html'}}

def records():
    DATA.mkdir(parents=True,exist_ok=True)
    return [json.loads(p.read_text()) for p in sorted(DATA.glob('*.json'))]

def _load(cid):
    if not cid.startswith('rea-') or not cid[4:].isalnum():
        raise ValueError('invalid candidate id')
    return DATA/f'{cid}.json'

def validate_world(world):
    if not isinstance(world,dict) or not isinstance(world.get('cells'),list) or not 0<len(world['cells'])<=50000:
        raise ValueError('需要有效的场景 World 快照')
    n=len(world['cells'])
    for field in ('solid','rock','color','river'):
        if not isinstance(world.get(field),list) or len(world[field])!=n:
            raise ValueError('场景数组长度不一致：'+field)
    for field in ('solid','rock'):
        if any(type(v) is not int or not 0<=v<=4095 for v in world[field]):
            raise ValueError('REA 体素位图最多支持12层')
    for i,c in enumerate(world['cells']):
        if c.get('id')!=i or not isinstance(c.get('edgeNeighbors'),list) or any(type(v) is not int or not -1<=v<n for v in c['edgeNeighbors']):
            raise ValueError('场景拓扑不合法')
    allowed={'shrubtex','watervol','lighthouse','contours','smoke'}
    if any(k not in allowed or type(v) is not bool for k,v in world.get('presentation',{}).items()):
        raise ValueError('显示层状态不合法')

async def execute(body):
    before=body['world'];steps=body['steps']
    validate_world(before)
    if not isinstance(steps,list) or not steps or len(steps)>32:
        raise ValueError('请选择 1–32 个能力步骤')
    descriptions=(await bridge.call('describe',{}))['voxel']
    by_id={c['id']:c for c in descriptions}
    for step in steps:
        if not isinstance(step,dict) or step.get('id') not in by_id:
            raise ValueError('管线中含有未登记能力')
        meta=by_id[step['id']]
        for key,value in step.get('params',{}).items():
            spec=meta.get('params',{}).get(key)
            if not spec or type(value) not in (int,float) or not spec.get('min',float('-inf'))<=value<=spec.get('max',float('inf')):
                raise ValueError('能力参数超出范围：'+key)
    context=body.get('context',{})
    mask=context.get('allowed')
    if mask is not None and any(by_id[s['id']].get('kind2')=='layer' for s in steps):
        raise ValueError('显示层作用于整个场景，请清除局部范围后执行')
    if mask is not None and (len(mask)!=len(before['cells']) or any(v not in (0,1) for v in mask)):
        raise ValueError('作用范围与场景不匹配')
    res=await bridge.call('voxel.run',{'world':before,'steps':steps,'ctx':context})
    if any(r.get('status')=='error' for r in res['report']):
        raise ValueError('能力执行失败，未生成可采用的候选')
    after={**before,**res['world']}
    presentation=dict(before.get('presentation',{}))
    scanned=None
    for report in res['report']:
        layer=report.get('layer3d')
        if layer:
            if scanned is None:
                scanned=await bridge.call('voxel.scan',{'world':after,'ctx':context})
            match=next((m for m in scanned if m['id']==report['id']),{})
            if match.get('count',0):presentation[layer]=True
            elif report.get('status')=='layer':report['status']='no-target'
    if 'presentation' in before or presentation:after['presentation']=presentation
    effective=digest(after)!=digest(before)
    # A separate Node process verifies the candidate, not the executor's own assertion.
    ver=await bridge.call('voxel.verify',{'before':before,'after':after,
                                        'opts':{'allowed':context.get('allowed'),'touchesBuilt':any(by_id[s['id']].get('touchesBuilt',False) for s in steps)}})
    row={'id':'rea-'+uuid.uuid4().hex,'at':time.time(),'goal':body.get('goal',''),
         'before':before,'after':after,'revision':digest(before),'steps':steps,
         'report':res['report'],'verification':ver,'status':'candidate','effective':effective}
    DATA.mkdir(parents=True,exist_ok=True)
    _load(row['id']).write_text(json.dumps(row,ensure_ascii=False))
    ExperienceStore(DATA/'experience.jsonl').add({'id':row['id'],'steps':steps,'verify':ver.get('status'),'outcome':'previewed'})
    return row

def decide(cid,body):
    path=_load(cid);row=json.loads(path.read_text());action=body['action']
    current=body.get('world')
    if action=='adopt':
        if row['status']!='candidate' or row['verification'].get('status')!='pass' or row.get('effective') is False:
            raise ValueError('只有验证全部通过的待选候选才能采用')
        if current is None or digest(current)!=row['revision']:
            raise ValueError('场景已变化或未提供当前 World，请重新生成候选')
        row['status']='adopted';world=row['after']
    elif action=='undo':
        if row['status']!='adopted' or current is None or digest(current)!=digest(row['after']):
            raise ValueError('当前场景与采用版本不一致，禁止覆盖后续修改')
        row['status']='reverted';world=row['before']
    elif action=='discard':
        if row['status']!='candidate':raise ValueError('只能放弃待选候选')
        row['status']='discarded';world=None
    else:raise ValueError('未知操作')
    path.write_text(json.dumps(row,ensure_ascii=False))
    ExperienceStore(DATA/'experience.jsonl').add({'id':cid,'steps':row['steps'],'verify':row['verification'].get('status'),'outcome':row['status']})
    return {'candidate':row,'world':world}
