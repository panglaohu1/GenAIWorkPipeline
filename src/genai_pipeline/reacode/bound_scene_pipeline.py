"""Independent scene workflow with an immutable handoff from its parent planet workflow."""
import asyncio,hashlib,json,uuid
from .. import config
from ..capabilities.bridge import node_path
from ..llm import LLM

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def source(ctx):
    binding=ctx.options['planet_binding'];rid=binding['planet_run'];run=ctx.store.get_run(rid)
    if not run or run['mode']!='rea_planet' or run['status'] in ('running','pending'):raise ValueError('来源球管线尚未完成')
    directory=config.resolve(config.get('pipeline.runs_dir','runs'))/rid
    state=json.loads((directory/'state.json').read_text())['rea_planet']
    if state.get('version')!=binding['terrain_version']:raise ValueError('来源球地形版本已变化；场景管线禁止继续使用旧区域')
    return binding,directory,state
async def handoff(ctx):
    binding,directory,state=source(ctx);data=json.loads((directory/'planet.json').read_text());lake=binding['lake_index']
    if type(lake) is not int or lake<0 or lake>=len(data.get('inlandWater',[])) or data['inlandWater'][lake].get('frame',{}).get('kind')!='local-tangent-plane':raise ValueError('场景输入必须是球管线中的有效平面湖区')
    checks=data['checks']
    if not checks.get('finite') or not checks.get('closed') or checks.get('euler')!=2:raise ValueError('来源球的几何检查未通过')
    before=(directory/'lake-scenes.json').read_bytes() if (directory/'lake-scenes.json').exists() else b'[]'
    (ctx.dir/'planet-input.json').write_text(json.dumps(data))
    snapshot={'binding':binding,'frame':data['inlandWater'][lake]['frame'],'before':json.loads(before),'before_hash':hashlib.sha256(before).hexdigest()}
    (ctx.dir/'handoff.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
    ctx.state['bound_scene']={'binding':binding,'status':'handoff_received','steps':ctx.options['steps']};ctx.save();ctx.artifact('spec',ctx.dir/'handoff.json','球管线交接的区域、版本与坐标');ctx.emit('result','已接收来源球管线的真实湖区，不创建独立示例World',binding)
async def plan(ctx):
    source(ctx);step=ctx.options['steps'][0];p=step.get('params',{})
    def check(value):
        if not isinstance(value,dict) or type(value.get('count')) is not int or not 1<=value['count']<=32 or type(value.get('floors')) is not int or not 1<=value['floors']<=5:return 'count需为1–32整数，floors需为1–5整数'
    if p.get('use_model',True):
        answer=await LLM('main').json([{'role':'system','content':'你是本地场景管线规划器。输入是已完成球管线中的真实平面湖区。唯一能力planet.lake-town：REA房屋、湖底承托台基、局部平面坐标映射。后两项固定执行，不得关闭；不能改变球地形或虚构验收。只选择count整数1–32、floors整数1–5，输出JSON。'},{'role':'user','content':json.dumps({'goal':ctx.run['brief'],'preferred_count':p.get('count',8),'preferred_floors':p.get('floors',2)},ensure_ascii=False)}],validate=check,thinking_budget=512,retries=1,on_delta=ctx.delta_emitter())
    else:answer={'count':p.get('count',8),'floors':p.get('floors',2)}
    error=check(answer)
    if error:raise ValueError(error)
    plan={'count':answer['count'],'floors':answer['floors'],'rationale':f"本地场景管线规划{answer['count']}栋、{answer['floors']}层；固定使用REA模块、湖底承托与平面坐标映射。"}
    ctx.state['bound_scene'].update(plan=plan,status='planned');ctx.save();ctx.emit('result',plan['rationale'],plan)
async def execute(ctx):
    binding,_,_=source(ctx);plan=ctx.state['bound_scene']['plan'];scene_id='lake-town-'+uuid.uuid4().hex[:12];out=ctx.dir/'scene-candidate.json'
    proc=await asyncio.create_subprocess_exec(node_path(),str(config.ROOT/'src/genai_pipeline/reacode/lake-town.mjs'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    payload={'input':str(ctx.dir/'planet-input.json'),'out':str(out),'options':{'lakeIndex':binding['lake_index'],'count':plan['count'],'floors':plan['floors'],'terrainVersion':binding['terrain_version'],'sceneId':scene_id}}
    try:_,err=await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()),90)
    except asyncio.TimeoutError:proc.kill();await proc.wait();raise RuntimeError('场景能力执行超时')
    if proc.returncode:raise RuntimeError(err.decode()[-1200:])
    scene=json.loads(out.read_text());scene['plan']=plan;scene['source_scene_run']=ctx.id;out.write_text(json.dumps(scene,ensure_ascii=False))
    ctx.state['bound_scene'].update(candidate_id=scene_id,status='built');ctx.save();ctx.artifact('scene',out,'真实湖区建筑候选');ctx.emit('result','场景能力执行完成，等待独立几何验收',{'buildings':len(scene['buildings'])})
async def review(ctx):
    binding,directory,_=source(ctx)
    out=ctx.dir/'scene-verification.json'
    proc=await asyncio.create_subprocess_exec(node_path(),str(config.ROOT/'scripts/verify_lake_scene.mjs'),str(ctx.dir/'planet-input.json'),str(ctx.dir/'scene-candidate.json'),str(out),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:_,err=await asyncio.wait_for(proc.communicate(),90)
    except asyncio.TimeoutError:proc.kill();await proc.wait();raise RuntimeError('场景独立验收超时')
    if proc.returncode:raise RuntimeError(err.decode()[-1200:])
    source(ctx);scene=json.loads((ctx.dir/'scene-candidate.json').read_text());handoff=json.loads((ctx.dir/'handoff.json').read_text())
    candidate={'status':'candidate','scene':scene,'before':handoff['before'],'before_hash':handoff['before_hash'],'scene_run':ctx.id}
    target=directory/(scene['id']+'.candidate.json');tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(candidate,ensure_ascii=False));tmp.replace(target)
    report=json.loads(out.read_text());ctx.state['bound_scene'].update(status='candidate_ready',verification=report);ctx.save();ctx.artifact('report',out,'独立进程：平面、承托、净空检查');ctx.emit('review','场景管线验收完成，候选尚未写回来源球；采用后才写回',report)
