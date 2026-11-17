"""Scene workshop -> immutable recipe -> local planner -> capability worker -> render check."""
import asyncio
import json
import os
from .. import config
from ..llm import LLM
from ..capabilities import bridge
from . import workshop as W
SCENES={'fusion','terrain','town','badnorth'}
def validate_recipe(recipe):
    if not isinstance(recipe,dict) or recipe.get('scene') not in SCENES:
        raise ValueError('请先在场景工坊保存有效场景配方')
    W.validate_world(recipe.get('world'))
    steps=recipe.get('steps')
    if not isinstance(steps,list) or not 1<=len(steps)<=32 or any(not isinstance(s,dict) or not isinstance(s.get('id'),str) or not isinstance(s.get('params',{}),dict) for s in steps):
        raise ValueError('场景配方需要1–32个已启用的能力节点')
    return recipe
async def plan(ctx):
    recipe=validate_recipe(ctx.options.get('scene_recipe'))
    path=ctx.dir/'scene-recipe.json';path.write_text(json.dumps(recipe,ensure_ascii=False,indent=2))
    descriptions=(await bridge.call('describe',{}))['voxel'];known={c['id']:c for c in descriptions}
    if any(s['id'] not in known for s in recipe['steps']):raise ValueError('配方中包含未注册的能力')
    facts={'scene':recipe['scene'],'cells':len(recipe['world']['cells']),'steps':recipe['steps'],
           'goal':ctx.run['brief'],'capabilities':[known[s['id']] for s in recipe['steps']]}
    def check(value):
        if not isinstance(value,dict) or value.get('steps')!=recipe['steps']:return '必须逐项保留用户能力顺序和参数，不能添加或删除节点'
        if not isinstance(value.get('rationale'),str):return '需要中文 rationale'
    ctx.emit('info','读取当前场景快照与已保存管线，由本地模型编排执行说明',{'steps':recipe['steps'],'scene':recipe['scene']})
    output=await LLM(ctx.options.get('model') or 'main').json([
        {'role':'system','content':'你是本地 REA 管线导演。用户已固定能力顺序和参数。请基于真实能力说明给出执行理由和需要观察的问题，不能改写World或编造验收。输出JSON：steps（原样保留输入），rationale（中文）。'},
        {'role':'user','content':json.dumps(facts,ensure_ascii=False)}],validate=check,thinking_budget=1024,retries=2,on_delta=ctx.delta_emitter())
    ctx.state['rea_scene']={'plan':output,'scene':recipe['scene'],'input_revision':W.digest(recipe['world']),'runtime_verified':False}
    ctx.artifact('spec',path,'场景与能力配方快照');ctx.save();ctx.emit('result',output['rationale'])
async def execute(ctx):
    recipe=json.loads((ctx.dir/'scene-recipe.json').read_text())
    row=await W.execute({'world':recipe['world'],'steps':ctx.state['rea_scene']['plan']['steps'],
                         'goal':ctx.run['brief'],'context':recipe.get('context',{})})
    path=ctx.dir/'scene-candidate.json';path.write_text(json.dumps(row,ensure_ascii=False))
    ctx.state['rea_scene'].update(candidate_id=row['id'],verification=row['verification'],effective=row['effective'])
    ctx.artifact('candidate',path,'能力执行候选');ctx.save();ctx.emit('review',row['verification']['summary'],row['verification'])
    if row['verification']['status']!='pass':raise RuntimeError('候选没有全部通过独立验证，不能标记创作完成')
    if not row['effective']:raise RuntimeError('能力执行没有产生变化，请调整节点或参数')
async def review(ctx):
    out=ctx.dir/'scene-playtest';out.mkdir(exist_ok=True)
    env=dict(os.environ,REA_SCENE_RUN_DIR=str(ctx.dir),REA_SCENE_BASE='http://127.0.0.1:'+str(config.get('console.port',8701)))
    proc=await asyncio.create_subprocess_exec(bridge.node_path(),str(config.ROOT/'scripts/test_scene_pipeline.cjs'),cwd=str(config.ROOT),env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    try:stdout,_=await asyncio.wait_for(proc.communicate(),150)
    except asyncio.TimeoutError:
        proc.kill();await proc.wait();raise RuntimeError('场景浏览器验收超时')
    (out/'browser.log').write_bytes(stdout)
    if proc.returncode:raise RuntimeError('场景浏览器验收失败：'+stdout.decode()[-1200:])
    report=json.loads((out/'report.json').read_text())
    ctx.state['rea_scene'].update(runtime_verified=True,render_check=report,status='candidate_ready')
    ctx.artifact('screenshot',out/'scene.png','按管线构建的真实场景');ctx.artifact('report',out/'report.json','真实场景回读核对')
    ctx.save();ctx.emit('result','场景管线创作完成：候选与原快照绑定，可回工坊对照和采用；此检查不代表战斗验收',report)
