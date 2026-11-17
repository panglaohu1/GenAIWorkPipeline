/** Bound lake scenes use the same workshop shell, with an explicit spherical-snapshot adapter. */
const $=s=>document.querySelector(s),params=new URLSearchParams(location.search);
const planet=params.get('planet'),version=params.get('terrain_version'),lake=Number(params.get('lake'));
const scopedStyle=document.createElement('style');scopedStyle.textContent='.scope p{white-space:pre-line;overflow-wrap:anywhere}';document.head.append(scopedStyle);
const key='genai.rea.bound-lake.'+planet+'.'+lake,frame=$('#scene');
let data=null,candidate=null,adopted=null,busy=false,compare=false;
let recipe={count:Number(params.get('count')||8),floors:Number(params.get('floors')||2),use_model:params.get('model')!=='0'};
try{const saved=JSON.parse(localStorage.getItem(key));if(saved?.terrain_version===version)recipe={...recipe,...saved.params}}catch{}
function text(selector,value){$(selector).textContent=value}
function notice(value){text('#notice',value)}
function log(value){const li=document.createElement('li'),time=document.createElement('time'),span=document.createElement('span');time.textContent=new Date().toLocaleTimeString('zh-CN');span.textContent=value;li.append(time,span);$('#activity').prepend(li);text('#historyCount',$('#activity').children.length+' 条记录')}
async function api(path,body){const response=await fetch(path,{cache:'no-store',...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})}),result=await response.json();if(!response.ok)throw Error(response.status===401?'请在本页右上角登录，当前星球与湖区会保留。':result.detail||'请求失败');return result}
const globe='/static/planet-studio.html?'+new URLSearchParams({run:planet,lake:String(lake)});
for(const a of document.querySelectorAll('a[href="/static/planet-studio.html"]'))a.href=globe;
text('#sceneSubtitle','当前星球的真实湖区 → 本地模型编排 → REA能力候选 → 验证 → 采用写回同一星球');
$('#sceneSelect').replaceChildren(new Option('星球 '+planet+' / 湖泊 '+(lake+1),'bound-lake'));$('#sceneSelect').disabled=true;
$('#goal').value=params.get('brief')||'在当前球面湖区建造彩色水上小镇，保留真实湖底承托。';
text('#scopeText','星球 '+planet+'\n湖泊 '+(lake+1)+'\n地形版本 '+version);
text('#viewerTitle','实景 / 当前星球湖泊 '+(lake+1));text('#viewMode','绑定湖区 · 原星球预览');
$('#tierTabs').hidden=true;$('#categoryFilters').hidden=true;$('#search').hidden=true;
text('#capCount','1 项已接入');$('#capList').replaceChildren();
const card=document.createElement('article');card.className='cap selected';const h=document.createElement('h3');h.textContent='湖面建筑 · 球面适配';const p=document.createElement('p');p.textContent='planet.lake-town：复用REA多层房屋模块，读取当前湖面与湖底，生成球面台基与建筑。';card.append(h,p);$('#capList').append(card);
const limits=document.createElement('p');limits.className='muted';limits.textContent='示例岛屿的体素改造能力尚未接入此球面快照；当前不会启动另一份示例World。';$('#capList').append(limits);
$('#layerMap').replaceChildren();text('#layerMap','01 真实湖区读取 → 02 REA建筑与承托 → 03 几何验证 → 04 采用写回');
for(const id of ['pick','clearScope','loadSceneResult'])$('#'+id).hidden=true;
$('#nodeInspector').replaceChildren();const title=document.createElement('h2');title.textContent='湖面建筑 · 参数';$('#nodeInspector').append(title);
for(const [name,label,min,max]of [['count','建筑数量',1,32],['floors','层数',1,5]]){const holder=document.createElement('label');holder.className='param';holder.textContent=label;const input=document.createElement('input');input.id='bound-'+name;input.type='number';input.min=min;input.max=max;input.step='1';input.value=recipe[name];input.onchange=()=>{recipe[name]=Number(input.value);render()};holder.append(input);$('#nodeInspector').append(holder)}
const modelLabel=document.createElement('label'),modelInput=document.createElement('input');modelInput.type='checkbox';modelInput.id='bound-model';modelInput.checked=recipe.use_model;modelInput.onchange=()=>recipe.use_model=modelInput.checked;modelLabel.append(modelInput,' 使用本地模型编排数量与层数');$('#nodeInspector').append(modelLabel);
const source=document.createElement('p');source.className='description';source.textContent='模块来源：REA src/sphere/planet-town.js · storeyHouseModule。适配：engines/system-workshop/planet/lake-town.js。';$('#nodeInspector').append(source);
$('#systemTab').onclick=()=>{$('#systemPanel').hidden=false;$('#learningPanel').hidden=true};
$('#learnTab').onclick=()=>{$('#systemPanel').hidden=true;$('#learningPanel').hidden=false;$('#learningCards').replaceChildren();const card=document.createElement('article');card.className='learning-card';card.textContent='湖区场景适配：当前湖面与湖底三角形 → 多层建筑模块 → 局部平面坐标映射 → 台基承托和水面净空。此适配只覆盖已登记湖面建筑能力，不代表全部场景体素能力已支持球系统。';$('#learningCards').append(card)};
function render(){text('#pipeline','');const row=document.createElement('div');row.className='pipe-row';const number=document.createElement('span');number.className='step-no';number.textContent='01 / 建筑';const node=document.createElement('article');node.className='node selected';node.style.setProperty('--node-color','#d3a17d');const heading=document.createElement('h3');heading.textContent='湖面建筑与真实承托';const id=document.createElement('div');id.className='node-id';id.textContent='planet.lake-town';const detail=document.createElement('p');detail.textContent=recipe.count+' 栋 · '+recipe.floors+' 层 · 星球 '+planet+' / 湖泊 '+(lake+1);node.append(heading,id,detail);row.append(number,node);$('#pipeline').append(row);for(const id of ['createScene','generate'])$('#'+id).disabled=busy||!!candidate||!data;for(const input of $('#nodeInspector').querySelectorAll('input'))input.disabled=busy||!!candidate;$('#adopt').disabled=busy||!candidate;$('#discard').disabled=busy||!candidate;$('#compare').disabled=busy||!candidate;$('#undo').disabled=busy||!adopted;$('#edit').disabled=busy||!!candidate;text('#recipeStatus','配方绑定当前湖区及地形版本，候选采用前不写回')}
function preview(id=null){const query=new URLSearchParams({run:planet,lake:String(lake),embed:'1'});if(id)query.set('candidate',id);$('#viewerOverlay').hidden=false;frame.src='/static/planet-studio.html?'+query;frame.onload=()=>{const start=Date.now();const ready=()=>{try{const value=frame.contentWindow.planetPreview;if(value?.ready&&value.frameRendered){$('#viewerOverlay').hidden=true;text('#connection','● 已连接原星球湖区');text('#sceneStats','星球 '+planet+' · 湖泊 '+(lake+1)+' · 地形版本 '+version.slice(0,12));return}}catch{}if(Date.now()-start<20000)setTimeout(ready,200);else notice('湖区预览未就绪，请刷新状态；不会切换到示例场景')};ready()}}
async function refresh(){const result=await api('/api/reacode/planet-data/'+encodeURIComponent(planet));if(result.state.version!==version)throw Error('地形版本已变化，请返回球系统重新选择湖泊后进入管线。');if(result.planet.inlandWater?.[lake]?.kind!=='lake')throw Error('当前选择不是有效湖泊');data=result;log('读取同一星球版本与真实湖面，现有场景 '+(data.planet.sceneInstances||[]).length+' 个');render()}
function checks(scene){$('#checks').replaceChildren();for(const [key,label]of [['finite','顶点为有限坐标'],['allFoundationsSupported','台基承托到真实湖底'],['allDecksAboveWater','建筑基座高于水面']]){const p=document.createElement('p');p.textContent=(scene.checks[key]?'✓ ':'✕ ')+label;$('#checks').append(p)}text('#verifyStatus','几何检查通过');text('#candidateSummary','实际放置 '+scene.buildings.length+'/'+scene.requestedCount+' 栋。导航、道路与玩法未验收。');$('#checks').parentElement.querySelector('p.muted').textContent='读取当前湖面和湖底三角形，检查承托与水面净空；预览不代表道路或游戏验收。'}
async function generate(){if(busy||candidate)return;busy=true;render();notice('读取绑定湖区，本地执行能力并生成候选…');log('开始：当前湖泊 → '+(recipe.use_model?'本地模型编排 → ':'')+'REA建筑 → 几何验证');try{const task=await api('/api/reacode/planet-scene-runs/'+encodeURIComponent(planet),{terrain_version:version,lake_index:lake,steps:[{id:'planet.lake-town',params:{...recipe}}],brief:$('#goal').value});const next=new URL(location.href);next.searchParams.set('scene_run',task.id);history.replaceState(null,'',next);const result=await waitScenePipeline(task.id);if(!result)throw Error('Scene output already processed');candidate=result;compare=false;checks(result.scene);preview(result.candidate_id);text('#viewMode','湖区创作候选 · 尚未采用');notice('候选已生成，请对照原星球；采用后才写回。');log('生成候选 '+result.candidate_id+'，'+result.placed_count+' 栋；未写回原星球')}catch(e){notice(e.message);log('未生成候选：'+e.message);if(e.message.includes('登录'))showLogin()}finally{busy=false;render()}}
$('#createScene').onclick=generate;$('#generate').onclick=generate;$('#scan').onclick=async()=>{try{await refresh();notice('真实湖区版本有效，可以执行已适配能力')}catch(e){data=null;render();notice(e.message)}};
async function decide(action){const current=action==='undo'?adopted:candidate;if(!current||busy)return;busy=true;render();try{await api('/api/reacode/planet-lake-candidate/'+encodeURIComponent(planet)+'/'+current.candidate_id,{action,terrain_version:version});if(action==='adopt'){adopted=candidate;candidate=null}else if(action==='discard')candidate=null;else adopted=null;compare=false;preview();await refresh();notice(action==='adopt'?'已写回原星球，返回球系统可看到同一湖区建筑。':action==='undo'?'已撤销采用，恢复原湖区场景。':'已放弃候选，原星球未改变。');text('#viewMode',action==='adopt'?'已采用 · 同一星球湖区':'原星球湖区');log('候选操作：'+action)}catch(e){notice(e.message);log('操作被拦截：'+e.message)}finally{busy=false;render()}}
$('#adopt').onclick=()=>decide('adopt');$('#discard').onclick=()=>decide('discard');$('#undo').onclick=()=>decide('undo');$('#compare').onclick=()=>{if(!candidate)return;compare=!compare;preview(compare?null:candidate.candidate_id);text('#compare',compare?'返回候选':'对照原版');text('#viewMode',compare?'原星球对照':'湖区创作候选 · 尚未采用')};
$('#saveRecipe').onclick=()=>{localStorage.setItem(key,JSON.stringify({planet,terrain_version:version,lake,params:recipe,goal:$('#goal').value}));notice('已保存当前星球湖区的能力配方');log('保存绑定配方')};
$('#exportRecipe').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify({planet,terrain_version:version,lake,steps:[{id:'planet.lake-town',params:recipe}]},null,2)],{type:'application/json'}));a.download='bound-lake-scene.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};
$('#edit').textContent='返回球面编辑';$('#edit').onclick=()=>location.assign(globe);$('#play').textContent='全屏查看';$('#play').onclick=$('#fullscreen').onclick=()=>frame.requestFullscreen().catch(e=>notice(e.message));
text('#recommendations','当前只执行已接入球面适配的湖面建筑能力。其他能力不会悄悄运行在独立示例World。');
try{if(!planet||!version||!Number.isInteger(lake)||lake<0)throw Error('缺少星球、地形版本或湖区上下文');await refresh();preview();notice('已进入原星球的湖区能力管线，可调整参数、生成候选并采用写回。')}catch(e){notice(e.message);text('#connection','未绑定：'+e.message);data=null;render();$('#viewerOverlay').textContent=e.message}

// Townscaper 式湖区建造：点水面放一块、点楼顶叠层、点墙面往外长、右键削掉。
// 每次操作只替换这一片湖区的网格，不整页重载，视角保持不变。
let buildingEditing=false,buildColor=-1,buildQueue=[],buildPumping=false;
const BUILD_PALETTE=['#d97d59','#e3c47a','#739fac','#9cad7a','#ab8fb4'];
const sceneChannel=new BroadcastChannel('genai-planet-scenes');
const editor=document.createElement('section');
editor.className='panel-bar';
editor.id='boundBuildingEditor';
editor.hidden=true;
editor.style.cssText='flex-wrap:wrap;gap:10px;align-items:center';
let buildFloors=1;
const buildHint=document.createElement('span');
buildHint.className='muted';
buildHint.style.cssText='flex:1 1 300px;min-width:240px;white-space:normal;line-height:1.5';
buildHint.textContent='按住左键在湖面拖动 连续长楼 · 点楼顶 叠一层 · 点墙面 往外长 · 按住右键拖动 连续削层 · Alt 点击 涂当前颜色 · Esc 退出';
const buildCount=document.createElement('span');
buildCount.id='buildCount';
buildCount.className='badge';
buildCount.textContent='0 栋';
editor.append(buildCount);
editor.append(buildHint);
const floorWrap=document.createElement('label');
floorWrap.className='muted';
floorWrap.style.cssText='display:inline-flex;gap:4px;align-items:center';
floorWrap.append(document.createTextNode('层数'));
const floorSelect=document.createElement('select');
floorSelect.id='buildFloors';
floorSelect.style.cssText='width:64px;padding:3px 4px';
for(const n of [1,2,3,4,5]){
 const option=document.createElement('option');
 option.value=String(n);
 option.textContent=n+' 层';
 floorSelect.append(option);
}
floorSelect.onchange=()=>{
 buildFloors=Number(floorSelect.value)||1;
 frame.contentWindow?.planetLakeEditor?.setFloors?.(buildFloors);
};
floorWrap.append(floorSelect);
editor.append(floorWrap);
const swatchRow=document.createElement('span');
swatchRow.style.cssText='display:inline-flex;gap:5px;align-items:center';
const swatchLabel=document.createElement('span');
swatchLabel.className='muted';
swatchLabel.textContent='配色';
swatchRow.append(swatchLabel);
function paintSwatches(){
 for(const button of swatchRow.querySelectorAll('button'))button.style.outline=Number(button.dataset.color)===buildColor?'2px solid #1f6f8b':'none';
}
for(const [value,label]of [[-1,'自动'],[0,''],[1,''],[2,''],[3,''],[4,'']]){
 const button=document.createElement('button');
 button.className='secondary';
 button.dataset.color=String(value);
 button.title=value<0?'按格子自动配色':'固定使用该颜色';
 if(value<0)button.textContent='自动';
 else{
  button.style.cssText='width:22px;height:22px;padding:0;border-radius:50%;background:'+BUILD_PALETTE[value]+';border:1px solid #b9cdd5';
  button.setAttribute('aria-label','颜色 '+(value+1));
 }
 button.onclick=()=>{
  buildColor=value;
  paintSwatches();
  frame.contentWindow?.planetLakeEditor?.setColor?.(value);
 };
 swatchRow.append(button);
}
paintSwatches();
editor.append(swatchRow);
const undoBuilding=document.createElement('button');
undoBuilding.id='undoBuilding';
undoBuilding.className='secondary';
undoBuilding.textContent='撤销上一步';
undoBuilding.onclick=()=>{
 buildQueue.push({action:'undo'});
 pumpBuilds();
};
editor.append(undoBuilding);
$('.viewport-panel .panel-bar').after(editor);

function activeTown(){
 return data?.planet.sceneInstances?.find(s=>s.lakeIndex===lake);
}
function syncEditorState(){
 const town=activeTown();
 buildCount.textContent=(town?.buildings?.length??0)+' 栋 / 64';
 const ready=Boolean(buildingEditing&&town&&frame.contentWindow?.planetLakeEditor?.applyScene);
 undoBuilding.disabled=!ready||buildPumping;
 swatchRow.querySelectorAll('button').forEach(button=>{
  button.disabled=!ready;
 });
}
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'&&buildingEditing){
  event.preventDefault();
  $('#edit').click();
 }
});
function bindEditor(attempt=0){
 const town=activeTown();
 if(!buildingEditing||!town)return;
 const editorApi=frame.contentWindow?.planetLakeEditor;
 if(!editorApi?.enable){
  if(attempt<60)setTimeout(()=>bindEditor(attempt+1),200);
  return;
 }
 if(!editorApi.enable(town.id,{
  color:buildColor,floors:buildFloors
 })&&attempt<60)setTimeout(()=>bindEditor(attempt+1),200);
 syncEditorState();
}
function describeOperation(operation){
 if(operation.action==='undo')return '撤销上一步';
 if(operation.action==='delete')return '拆掉一栋';
 if(operation.action==='add')return '放置 '+operation.floors+' 层';
 if(operation.action==='update')return operation.floors?'改成 '+operation.floors+' 层':operation.color!==undefined?'换颜色':'改层高';
 return operation.action;
}
async function runOperation(operation){
 const town=activeTown();
 if(!town)throw Error('当前湖区没有已采用场景');
 const result=await api('/api/reacode/planet-lake-edit/'+encodeURIComponent(planet)+'/'+town.id,{
  terrain_version:version,scene_revision:town.sceneRevision,...operation
 });
 const list=data?.planet?.sceneInstances||[];
 const index=list.findIndex(s=>s.id===town.id);
 if(index>=0)list[index]=result.scene;
 frame.contentWindow?.planetLakeEditor?.applyScene?.(result.scene);
 try{
  checks(result.scene);
 }
 catch{
 }
 adopted=null;
 log('建造 · '+describeOperation(operation)+' → 现在 '+result.scene.buildings.length+' 栋');
 notice('已写回原星球：'+result.scene.buildings.length+' 栋建筑。撤销可回退。');
 syncEditorState();
}
async function pumpBuilds(){
 if(buildPumping)return;
 buildPumping=true;
 syncEditorState();
 try{
  while(buildQueue.length){
   const operation=buildQueue.shift();
   try{
    await runOperation(operation);
   }
   catch(error){
    notice(error.message);
    log('建造被拒绝：'+error.message);
    const town=activeTown();
    if(town)frame.contentWindow?.planetLakeEditor?.applyScene?.(town);
    if(error.message.includes('登录'))showLogin();
   }
  }
 }
 finally{
  buildPumping=false;
  syncEditorState();
 }
}
function toOperation(value){
 if(value.action==='paint')return {
  action:'update',building_id:value.building_id,color:value.color
 };
 if(value.action==='stack')return {
  action:'update',building_id:value.building_id,floors:value.floors
 };
 if(value.action==='erase')return value.floors>1?{
  action:'update',building_id:value.building_id,floors:value.floors-1
 }:{
  action:'delete',building_id:value.building_id
 };
 return {
  action:'add',x:value.x,z:value.z,floors:value.floors,color:value.color
 };
}
$('#edit').textContent='进入建筑编辑';
$('#edit').onclick=()=>{
 if(candidate)return notice('先采用或放弃当前候选，再编辑建筑');
 if(!activeTown())return notice('当前湖区尚无已采用的场景，请先生成并采用候选');
 buildingEditing=!buildingEditing;
 editor.hidden=!buildingEditing;
 $('#edit').textContent=buildingEditing?'退出建筑编辑':'进入建筑编辑';
 if(buildingEditing){
  bindEditor();
  text('#viewMode','建筑编辑 · 点水面建造');
  notice('Townscaper 式建造：左键点水面放一块，点楼顶叠层，点墙面往该侧延伸，右键或 Shift 点击削层，Alt 点击涂色。每一步都写回原星球。');
 }else{
  frame.contentWindow?.planetLakeEditor?.disable?.();
  text('#viewMode','原星球湖区');
 }
 syncEditorState();
};
window.addEventListener('message',event=>{
 const value=event.data;
 if(event.origin!==location.origin||event.source!==frame.contentWindow)return;
 if(value?.type==='genai-lake-notice')return notice(String(value.text||''));
 if(value?.type!=='genai-lake-edit-intent'||!buildingEditing)return;
 if(value.planet!==planet)return notice('这次点击来自其他星球场景，已忽略。');
 if(value.terrainVersion!==version)return notice('地形版本已变化，请重新从球系统进入后再编辑。');
 const town=activeTown();
 if(!town||value.sceneId!==town.id)return notice('场景与当前湖区不一致，请退出后重新进入建筑编辑。');
 if(typeof value.building_id==='string'&&value.building_id.indexOf('pending:')===0)return notice('上一块还在保存，稍等一下再点这一栋。');
 if(buildQueue.length>8)return notice('建造速度超过保存速度，正在排队，请稍候。');
 buildQueue.push(toOperation(value));
 pumpBuilds();
});
frame.addEventListener('load',()=>{
 if(buildingEditing)bindEditor();
});
sceneChannel.addEventListener('message',async event=>{
 if(event.data?.planet!==planet||busy||candidate||buildingEditing)return;
 try{
  await refresh();
  await frame.contentWindow?.planetLakeEditor?.refresh?.();
 }
 catch(error){
  notice(error.message);
 }
});
// Login stays in this page so authentication cannot lose the selected planet/lake.
const accountButton=document.createElement('button');accountButton.className='secondary';accountButton.id='boundAccount';accountButton.textContent='登录本机账户';$('.top').append(accountButton);
const loginDialog=document.createElement('dialog');loginDialog.style.cssText='border:1px solid #cfe3ea;border-radius:12px;padding:24px;max-width:380px';loginDialog.innerHTML='<form id="boundLoginForm"><h2>登录本机账户</h2><p>登录后留在当前星球湖区，不切换示例场景。</p><label>用户名<input name="username" autocomplete="username" required></label><label>密码<input name="password" type="password" autocomplete="current-password" required></label><p id="boundLoginError" role="alert"></p><button type="submit" class="primary">登录并继续</button> <button type="button" id="cancelBoundLogin">取消</button><p><a href="/" target="_blank" rel="noopener">没有账户？在新窗口注册</a></p></form>';document.body.append(loginDialog);
function showLogin(){if(!loginDialog.open)loginDialog.showModal()}
accountButton.onclick=showLogin;$('#cancelBoundLogin').onclick=()=>loginDialog.close();
async function accountStatus(){try{const status=await api('/api/auth/status');accountButton.textContent=status.user?'已登录：'+status.user:'登录本机账户';accountButton.disabled=Boolean(status.user)}catch{accountButton.disabled=false}}
$('#boundLoginForm').onsubmit=async event=>{event.preventDefault();const form=event.target,button=form.querySelector('button[type="submit"]');button.disabled=true;try{await api('/api/auth/login',{username:form.elements.username.value,password:form.elements.password.value});form.elements.password.value='';loginDialog.close();await accountStatus();notice('已登录，当前星球湖区保持不变。')}catch(e){text('#boundLoginError',e.message)}finally{button.disabled=false}};
window.addEventListener('focus',accountStatus);await accountStatus();

const stageNames={bound_scene_handoff:'接收球管线区域',bound_scene_plan:'本地场景编排',bound_scene_execute:'场景能力执行',bound_scene_review:'独立场景验收'};
async function waitScenePipeline(runId){$('#sceneRunPanel').hidden=false;$('#sceneRunLink').href='/#'+runId;$('#sceneRunLink').textContent='查看独立场景管线任务';for(;;){const task=await api('/api/reacode/bound-scene-runs/'+encodeURIComponent(runId));text('#sceneRunStatus',task.run.status+' · '+(stageNames[task.run.current_stage]||''));text('#sceneRunSummary',task.stages.map(s=>(stageNames[s.name]||s.name)+'：'+s.status).join(' → '));notice('场景管线任务 '+runId+' · '+(stageNames[task.run.current_stage]||task.run.status));if(['failed','cancelled','interrupted'].includes(task.run.status))throw Error(task.run.error||'场景管线任务未完成');if(task.run.status==='done'){const st=task.state;if(st.writeback&&st.writeback!=='candidate')return null;const payload=await api('/api/reacode/planet-data/'+encodeURIComponent(planet)+'?candidate='+encodeURIComponent(st.candidate_id));const scene=payload.planet.sceneInstances.find(s=>s.id===st.candidate_id);if(!scene)throw Error('场景管线没有产生有效候选');return {candidate_id:st.candidate_id,scene,placed_count:scene.buildings.length,requested_count:scene.requestedCount,scene_run:runId}}await new Promise(resolve=>setTimeout(resolve,1500))}}
async function restoreSceneTask(runId){busy=true;render();try{const result=await waitScenePipeline(runId);if(result){candidate=result;checks(result.scene);preview(result.candidate_id);notice('已恢复独立场景管线的候选，可采用写回原星球')}else notice('场景管线产物已处理，当前显示原星球实际场景')}catch(e){notice(e.message);if(e.message.includes('登录'))showLogin()}finally{busy=false;render()}}
if(params.get('scene_run'))restoreSceneTask(params.get('scene_run'));
