"""Local LLM selects supported engine controls; geometry and battle stay deterministic code."""
import json
from urllib.parse import urlencode
from ..llm import LLM
from .workshop import ROOT
THEMES={'summer','dawn','rain','dusk','autumn'}
def validate(p):
    if not isinstance(p,dict):return '需要 JSON 对象'
    if p.get('theme') not in THEMES:return 'theme 必须为 summer/dawn/rain/dusk/autumn'
    if type(p.get('seed')) is not int or not 1<=p['seed']<=999999:return 'seed 必须为 1–999999 整数'
    if type(p.get('waves')) is not int or not 1<=p['waves']<=8:return 'waves 必须为 1–8 整数'
    if not isinstance(p.get('rationale'),str):return '需要 rationale 说明玩法编排理由'
async def plan(ctx):
    caps=['badnorth/island.js','badnorth/battle.js','world/navgraph.js','build/buildings.js','world/worldmesh.js']
    prompt='''你是本地 REA 游戏导演。只使用已实现模块构建一个可玩的 Bad North 守岛关卡：三队守军、步兵和弓手、长船登陆、自动战斗、保卫房屋。建筑使用共享 Townscaper 网格与建筑模块。无需目标图，不生成网格代码，不调用图像服务。输出 JSON：seed（1–999999整数），theme（summer/dawn/rain/dusk/autumn），waves（1–8整数），rationale（中文解释）。首次试玩优先3波，选择可读的夏季光照。不要声称完成了尚未执行的验证。'''
    ctx.emit('info','本地 LLM 根据 REA 现有模块编排关卡，不进行目标图趋近',{'modules':caps})
    p=await LLM('main').json([{'role':'system','content':prompt},{'role':'user','content':ctx.run['brief']}],thinking_budget=1024,retries=2,validate=validate,on_delta=ctx.delta_emitter())
    ctx.state['rea_game']={'plan':p,'modules':caps,'runtime_verified':False}
    path=ctx.dir/'engine-plan.json';path.write_text(json.dumps(p,ensure_ascii=False,indent=2))
    ctx.artifact('spec',path,'本地 LLM 引擎配方');ctx.save();ctx.emit('result',p['rationale'],p)
async def build(ctx):
    p=ctx.state['rea_game']['plan']
    missing=[x for x in ctx.state['rea_game']['modules'] if not (ROOT/'src'/x).is_file()]
    if missing:raise RuntimeError('缺少模块：'+str(missing))
    url='/engines/REACodeRefactoring/index.html?'+urlencode({'scene':'badnorth','seed':p['seed'],'theme':p['theme'],'waves':p['waves'],'play':'1'})
    ctx.state['rea_game']['url']=url;ctx.save()
    path=ctx.dir/'play.html';path.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>北境守岛</title><style>body{margin:0;background:#dfebec}header{padding:12px;font:16px system-ui}iframe{width:100%;height:calc(100vh - 50px);border:0}</style><header>北境守岛 · 点选小队，再点地面移动 · 空格暂停 · R 换岛</header><iframe title="北境守岛游戏" src="'+url+'"></iframe>')
    ctx.artifact('game',path,'打开北境守岛');ctx.emit('result','REA 已组装地形、建筑、导航和战斗，等待真实浏览器验收',{'url':url})
async def review(ctx):
    import asyncio, os
    from .. import config
    from ..capabilities.bridge import node_path
    target=ctx.dir/'playtest';target.mkdir(exist_ok=True)
    env=dict(os.environ,GAME_URL='http://127.0.0.1:'+str(config.get('console.port',8701))+ctx.state['rea_game']['url'],BADNORTH_OUT=str(target))
    proc=await asyncio.create_subprocess_exec(node_path(),str(config.ROOT/'scripts/test_badnorth.cjs'),cwd=str(config.ROOT),env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    stdout,_=await asyncio.wait_for(proc.communicate(),timeout=150)
    (target/'test.log').write_bytes(stdout)
    if proc.returncode:raise RuntimeError('浏览器验收未通过：'+stdout.decode()[-1500:])
    report=json.loads((target/'runtime.json').read_text())
    ctx.state['rea_game'].update(runtime_verified=True,status='smoke_passed',playtest=report)
    for name in ('start.png','landing.png','runtime.json'):
        ctx.artifact('screenshot' if name.endswith('.png') else 'report',target/name,'真实运行 · '+name)
    ctx.save();ctx.emit('review','真实浏览器冒烟通过：小队移动、长船登陆、可控胜负分支与无脚本异常；完整战役平衡尚未验收',report)
