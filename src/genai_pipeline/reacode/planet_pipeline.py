"""Global sphere first. Scene creation is intentionally not part of these stages."""
import asyncio,hashlib,json,os,subprocess
from .. import config
from ..llm import LLM
from ..capabilities.bridge import node_path

def validate(p):
    if not isinstance(p,dict):return '需要JSON'
    if type(p.get('seed')) is not int or not 1<=p['seed']<=999:return 'seed需要1–999整数'
    if type(p.get('radius')) not in (int,float) or not 80<=p['radius']<=400:return 'radius需要80–400'
    if type(p.get('seaLevel')) not in (int,float) or not -2<=p['seaLevel']<=2:return 'seaLevel需要-2–2'
    if not isinstance(p.get('rationale'),str):return '需要rationale说明'
def capability_catalog():
    result=subprocess.run([node_path(),str(config.ROOT/'src/genai_pipeline/reacode/planet-library.mjs'),'--catalog'],capture_output=True,text=True,timeout=15,check=True)
    return json.loads(result.stdout)

async def plan(ctx):
    library=capability_catalog()
    available=[c for c in library if c['executable']]

    explicit=ctx.options.get('planet_recipe')
    if explicit is not None:
        p={**explicit,'rationale':'用户通过地形菜单指定参数，直接执行本机能力库。'}
        error=validate(p)
        if error:raise ValueError(error)
    else:
        p=await LLM('main').json([{'role':'system','content':'你是本地球系统规划器。先构建全局球面地形，不创建建筑或孤立场景。输出JSON: seed整数1–999，radius80–400，seaLevel -2到2，rationale中文说明。首次验收优先seed7 radius160 seaLevel0以复用已通过编译配方。不要声称尚未执行的验证通过。'},{'role':'user','content':ctx.run['brief']+'\n球系统已接入的学习能力库（使用其参数范围规划，不可虚构能力）：'+json.dumps(available,ensure_ascii=False)}],validate=validate,thinking_budget=1024,retries=2,on_delta=ctx.delta_emitter())
    ctx.state['rea_planet']={'plan':p,'capability_catalog':library,'status':'planned'};ctx.save();ctx.emit('result',p['rationale'],p)
async def build(ctx):
    p=ctx.state['rea_planet']['plan'];recipe={k:p[k] for k in ('seed','radius','seaLevel')};recipe.update(subdivision=1,chartLimit=6,resolution=16)
    steps=[{'id':'planet.reseed','params':{'seed':p['seed']}},{'id':'planet.sea-level','params':{'level':p['seaLevel']}},{'id':'planet.surface-detail','params':{'resolution':16}}]
    # Capability bounds, not silent clipping, determine the supported recipe domain.
    out=ctx.dir/'planet.json'
    proc=await asyncio.create_subprocess_exec(node_path(),str(config.ROOT/'src/genai_pipeline/reacode/planet-build.mjs'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:stdout,err=await asyncio.wait_for(proc.communicate(json.dumps({'recipe':recipe,'steps':steps,'out':str(out)}).encode()),120)
    except asyncio.TimeoutError:proc.kill();await proc.wait();raise RuntimeError('星球编译超时')
    if proc.returncode:raise RuntimeError(err.decode()[-1500:])
    data=json.loads(out.read_text());version=hashlib.sha256(out.read_bytes()).hexdigest()
    ctx.state['rea_planet'].update(version=version,capabilities=data.get('capabilities',[]),stats=data['stats'],checks=data['checks'],navigation=data['navigation'],status='terrain_built')
    ctx.artifact('planet',out,'全球球面地形数据');ctx.save();ctx.emit('result','全球球面已构建，尚未创建场景',data['stats'])
async def review(ctx):
    target=ctx.dir/'planet-check';target.mkdir(exist_ok=True)
    env=dict(os.environ,PLANET_RUN=ctx.id,PLANET_OUT=str(target),PLANET_BASE='http://127.0.0.1:'+str(config.get('console.port',8701)))
    proc=await asyncio.create_subprocess_exec(node_path(),str(config.ROOT/'scripts/test_planet_studio.cjs'),env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    try:stdout,_=await asyncio.wait_for(proc.communicate(),120)
    except asyncio.TimeoutError:proc.kill();await proc.wait();raise RuntimeError('全球视图检查超时')
    (target/'browser.log').write_bytes(stdout)
    if proc.returncode:raise RuntimeError(stdout.decode()[-1500:])
    report=json.loads((target/'report.json').read_text());ctx.state['rea_planet'].update(status='terrain_checked',browser=report)
    ctx.artifact('screenshot',target/'planet.png','全球地形实际画面');ctx.artifact('report',target/'report.json','全球视图与选区检查');ctx.save();ctx.emit('review','全球球面闭合与浏览器选区通过；未进行角色环球通行或场景构建',report)
